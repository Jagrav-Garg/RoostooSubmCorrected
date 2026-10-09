"""Read-only account diagnosis and exports from the existing tick ledger.

No function in this module submits, cancels, or modifies an exchange order.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import time

from .api import ApiError, Roostoo

HKT = timezone(timedelta(hours=8))
IST = timezone(timedelta(hours=5, minutes=30))


def iso(milliseconds, zone=timezone.utc):
    return datetime.fromtimestamp(int(milliseconds) / 1000, zone).isoformat(timespec='seconds')


def get(db, key, default=None):
    row = db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def fills(db, mode):
    result = []
    for identity, timestamp, payload in db.execute('SELECT id,timestamp_ms,payload FROM fills ORDER BY timestamp_ms,id'):
        row = json.loads(payload)
        if row.get('mode', mode) == mode and float(row.get('quantity', 0)) > 0:
            result.append({'id': identity, 'timestamp_ms': timestamp, **row})
    return result


def daily_counts(db, now, mode):
    start = datetime.fromtimestamp(now / 1000, HKT).replace(hour=0, minute=0, second=0, microsecond=0)
    lower, upper = int(start.timestamp()*1000), int((start+timedelta(days=1)).timestamp()*1000)
    attempts, accepted, rejected, unfilled = 0, 0, 0, 0
    for status, payload, response in db.execute('SELECT status,payload,response FROM intents WHERE created_ms>=? AND created_ms<?', (lower, upper)):
        row = json.loads(payload)
        if row.get('mode', mode) != mode or row.get('side') != 'BUY':
            continue
        attempts += 1
        rejected += status == 'rejected'
        accepted += status in {'acknowledged', 'resting', 'done', 'canceled'}
        detail = (json.loads(response) if response else {}).get('OrderDetail', {})
        unfilled += detail.get('Status') in {'CANCELED', 'CANCELLED', 'REJECTED'} and float(detail.get('FilledQuantity', 0) or 0) == 0
    today_fills = [r for r in fills(db, mode) if lower <= r['timestamp_ms'] < upper]
    samples = get(db, 'tick_closed_samples', [])
    completed = sum(lower <= r['timestamp_ms'] < upper and r.get('mode', mode) == mode and r.get('cycle_closed', True) for r in samples)
    return {'hkt_day': start.date().isoformat(), 'entry_attempts_today': attempts,
            'accepted_entries_today': accepted, 'rejected_entries_today': rejected,
            'unfilled_terminal_entries_today': unfilled,
            'filled_entries_today': sum(r.get('side') == 'BUY' for r in today_fills),
            'filled_orders_today': len(today_fills), 'completed_cycles_today': completed}


def snapshot(db, mode, now):
    errors = []
    for ts, kind, payload in db.execute("SELECT timestamp_ms,kind,payload FROM events WHERE kind IN ('tick_api_pause','tick_order_rejected','tick_write_unknown','tick_cancel_rejected','tick_paused') ORDER BY id DESC LIMIT 5"):
        errors.append({'timestamp_ms': ts, 'event': kind, **json.loads(payload)})
    rows = fills(db, mode)
    samples = [r for r in get(db, 'tick_closed_samples', []) if r.get('mode', mode) == mode]
    return {'mode': mode, 'execution_source': 'signed_roostoo_account' if mode == 'paper-exchange' else 'local_shadow_simulation',
            'generated_at_utc': iso(now), 'generated_at_ist': iso(now, IST),
            'state': get(db, 'tick_state', {}), 'portfolio': get(db, 'tick_last_snapshot', {}),
            'last_decision': get(db, 'tick_last_decision', {}), 'daily_counts': daily_counts(db, now, mode),
            'total_recorded_filled_orders': len(rows),
            'total_recorded_completed_cycles': sum(r.get('cycle_closed', True) for r in samples),
            'recorded_realized_pnl_usd': sum(float(r['pnl']) for r in samples),
            'recent_errors': errors, 'fee_violation': get(db, 'tick_fee_violation'),
            'unresolved_writes': [{'intent': identity, 'status': status, 'payload': json.loads(payload)} for identity, status, payload in db.execute("SELECT id,status,payload FROM intents WHERE status IN ('submitted','acknowledged','unknown') ORDER BY created_ms")]}


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(text, encoding='utf-8')
    temp.replace(path)


def export_report(db, mode, destination, now):
    dest = Path(destination)
    report = snapshot(db, mode, now)
    lines = ['Roostoo tick bot summary', f"Execution source: {report['execution_source']}",
             f"Generated UTC: {report['generated_at_utc']}", f"Generated IST: {report['generated_at_ist']}",
             f"Phase: {report['state'].get('phase', 'not_started')}",
             f"Pair: {report['state'].get('pair', '')}",
             f"Last portfolio equity USD: {report['portfolio'].get('equity', 'unavailable')}",
             f"Last decision: {report['last_decision'].get('reason', 'unavailable')}"]
    lines.extend(f'{name}: {value}' for name, value in report['daily_counts'].items())
    lines.extend([f"Total recorded filled orders: {report['total_recorded_filled_orders']}",
                  f"Total recorded completed cycles: {report['total_recorded_completed_cycles']}",
                  f"Recorded realized P&L USD: {report['recorded_realized_pnl_usd']:.8f}",
                  f"Unresolved writes: {len(report['unresolved_writes'])}",
                  f"Fee violation: {report['fee_violation']}",
                  'Submitted or canceled unfilled orders are not completed trades.',
                  'These files are local exports. No email, Telegram or external delivery is configured.'])
    atomic_write(dest/'summary.txt', '\n'.join(lines)+'\n')
    atomic_write(dest/'status.json', json.dumps(report, indent=2, allow_nan=False)+'\n')
    fields = ['timestamp_utc', 'timestamp_ist', 'timestamp_ms', 'order_id', 'pair', 'side', 'quantity', 'price', 'fee_rate', 'commission_coin', 'commission_amount', 'role', 'status', 'pnl_usd', 'mode', 'config_hash', 'commit']
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for row in fills(db, mode):
        writer.writerow({**row, 'order_id': row.get('order_id', row['id']), 'timestamp_utc': iso(row['timestamp_ms']), 'timestamp_ist': iso(row['timestamp_ms'], IST)})
    atomic_write(dest/'trade_logs.csv', stream.getvalue())
    return report


def diagnose(config='tick_config.json', mode='paper-exchange', remote=False, output=None):
    from .tick import TickConfig
    c = TickConfig.load(config)
    now = int(time.time()*1000)
    remote_data = None
    if remote:
        if mode != 'paper-exchange':
            raise ValueError('Remote diagnosis belongs to paper-exchange mode')
        api = Roostoo(spacing=2.)
        if not api.authenticated:
            raise ValueError('Private Roostoo credentials are required for read-only account diagnosis')
        api.sync_time()
        remote_data = {'writes_performed': 0, 'read_errors': {}}
        def read(name, call):
            try:
                return call()
            except ApiError as error:
                remote_data['read_errors'][name] = str(error)
                return None
        info = read('exchange_info', api.exchange_info) or {}
        ticker = read('ticker', api.ticker) or {}
        remote_data.update({'wallet': read('balance', api.balance),
                            'pending_orders': read('pending_orders', lambda: api.query_orders(pending_only=True)),
                            'recent_orders': read('recent_orders', lambda: api.query_orders(limit=100)),
                            'short_positions': read('short_positions', api.short_positions),
                            'exchange_running': info.get('IsRunning'),
                            'pairs': {p: {'metadata': info.get('TradePairs', {}).get(p), 'quote': ticker.get('Data', {}).get(p)} for p in c.pairs}})
        now = api.now_ms()
        remote_data['server_time_utc'] = iso(now)
    path = Path(c.runtime_dir)/('tick_'+mode.replace('-', '_')+'.sqlite3')
    report = {'mode': mode, 'ledger_exists': path.exists(), 'ledger_path': str(path)}
    if path.exists():
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
        try:
            db.execute('BEGIN')
            report.update(export_report(db, mode, output, now) if output else snapshot(db, mode, now))
        finally:
            db.close()
    if remote_data is not None:
        report['remote'] = remote_data
        if output:
            atomic_write(Path(output)/'account_diagnosis.json', json.dumps(report, indent=2, allow_nan=False)+'\n')
    return report
