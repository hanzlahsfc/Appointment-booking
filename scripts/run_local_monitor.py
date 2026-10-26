"""Local operator launcher; no personal Chrome profile is ever used."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import load_config
from app.hosted.store import HostedStore
from app.site import BookingSelectors
import app.hosted.__main__ as cli

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--client', default='monitor')
    p.add_argument('--url', default=None)
    p.add_argument('--origin', action='append')
    p.add_argument('--category', default=None)
    p.add_argument('--location', default=None)
    p.add_argument('--selectors-file', type=Path)
    p.add_argument('--arm-booking', action='store_true', help='Allow reserve only; final submission remains manual')
    p.add_argument('--issue-code', action='store_true', help='Rotate code and revoke old dashboard sessions')
    p.add_argument('--poll-interval', type=int, default=15)
    args = p.parse_args()
    if args.poll_interval < 5:
        p.error('poll interval must be at least 5 seconds')
    cfg = load_config()
    cfg.hosted.db_path = cfg.data_dir / 'monitor-hosted.db'
    cfg.hosted.browsers_dir = cfg.data_dir / 'monitor-browsers'
    cfg.hosted.headless = False
    cfg.hosted.allow_mock_portal = False
    cfg.booking.mode = 'assist' if args.arm_booking else 'off'
    cfg.booking.dry_run = not args.arm_booking
    cfg.app.dry_run = not args.arm_booking
    store = HostedStore(cfg.hosted.db_path, cfg.db_path)
    try:
        client = store.client_by_username(args.client)
        selectors = json.loads(args.selectors_file.read_text(encoding='utf-8-sig')) if args.selectors_file else (client or {}).get('selectors', {})
        selected = BookingSelectors(**selectors)
        if args.arm_booking and not selected.configured:
            p.error('cannot arm booking: validated picker/slot/reserve selectors are missing')
        if client is None:
            client, code = store.add_client(username=args.client, display_name=args.client, profile_name=f'client:{args.client}', booking_url=args.url or 'https://pk-gr-services.gvcworld.eu/login/appointments/en', category=args.category or 'national_d_employment', location=args.location or 'lahore', allowed_origins=args.origin or ['https://pk-gr-services.gvcworld.eu'], selectors=selectors, auto_book=args.arm_booking)
        else:
            updates = {'selectors': json.dumps(selectors), 'auto_book': int(args.arm_booking)}
            for key, value in [('booking_url', args.url), ('category', args.category), ('location', args.location), ('allowed_origins', json.dumps(args.origin) if args.origin else None)]:
                if value is not None:
                    updates[key] = value
            query = ', '.join(f'{key}=?' for key in updates)
            store._conn.execute(f'UPDATE clients SET {query} WHERE id=?', (*updates.values(), client['id']))
            store._conn.commit()
            code = store.issue_code(client['id']) if args.issue_code else None
    finally:
        store.close()
    print('Dashboard: http://127.0.0.1:8081', flush=True)
    print('Mode:', cfg.booking.mode, flush=True)
    if code:
        print('Access code (private):', code, flush=True)
    if not selected.configured:
        print('BLOCKER: selectors missing; browser login works but availability monitoring cannot succeed.', flush=True)
    cli.load_config = lambda: cfg
    sys.argv = ['hosted', 'serve', '--booking-mode', cfg.booking.mode, '--no-booking-dry-run' if args.arm_booking else '--booking-dry-run', '--poll-interval', str(args.poll_interval)]
    return cli.main()

if __name__ == '__main__':
    raise SystemExit(main())
