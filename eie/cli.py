"""Command-line interface.

  python -m eie gui                      web GUI at http://127.0.0.1:8000
  python -m eie run                      one pass over the mailbox
  python -m eie poll --interval 60       keep polling
  python -m eie demo samples/sample_emails.json
  python -m eie emails
  python -m eie review list | show ID | approve ID --by NAME | reject ID --by NAME
  python -m eie review claim ID --by NAME | dismiss ID --by NAME --reason TEXT | forward ID --by NAME --team NAME
  python -m eie contacts FILE.csv        load booking contacts (booking_id,role,name,email,phone)
  python -m eie subscribe --url URL      create the Microsoft Graph webhook subscription
  python -m eie audit [--email-id N]
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .app import create_app
from .ingestion.file import JsonFileSource
from .models import Category
from .review import ReviewError


def _print_processed(results) -> None:
    if not results:
        print("No new emails.")
        return
    print(f"{'ID':>4}  {'CATEGORY':<13}{'CONF':>5}  {'ACTION':<17}{'DESTINATION':<19}{'STATUS':<15}SUBJECT")
    for r in results:
        print(f"{r.email_id:>4}  {r.category:<13}{r.confidence:>5}  {r.action:<17}{r.destination:<19}"
              f"{r.status:<15}{r.subject[:50]}")


def cmd_check_mailbox(app, args) -> None:
    """Connects and lists a few unread emails without processing or marking them."""
    from .ingestion import build_source

    s = app.settings
    print(f"Mailbox: {s.graph_mailbox or s.imap_user or '(not set)'}  source={s.email_source}")
    try:
        source = build_source(s)
        emails = source.fetch_unread(args.limit)
    except Exception as exc:
        sys.exit(f"FAILED: {exc}")
    print(f"OK - connected via {source.name}; showing {len(emails)} unread (nothing was processed or marked read)")
    for e in emails:
        print(f"  {e.received_at[:16]:<17}{e.sender[:35]:<37}{e.subject[:60]}")


def cmd_run(app, args) -> None:
    _print_processed(app.pipeline().run_once())


def cmd_poll(app, args) -> None:
    pipeline = app.pipeline()
    print(f"Polling every {args.interval}s (Ctrl+C to stop)")
    while True:
        _print_processed(pipeline.run_once())
        time.sleep(args.interval)


def cmd_demo(app, args) -> None:
    _print_processed(app.pipeline(JsonFileSource(args.file)).run_once())


def cmd_emails(app, args) -> None:
    for e in app.store.list_emails(args.limit):
        print(f"{e['id']:>4}  {e['status']:<15}{e['category'] or '-':<13}{e['confidence'] or 0:>4}  "
              f"{(e['sender'] or '')[:30]:<32}{(e['subject'] or '')[:50]}")


def cmd_review(app, args) -> None:
    if args.review_cmd == "list":
        items = app.store.list_reviews(None if args.status == "all" else args.status, args.kind)
        if not items:
            print("Review queue is empty.")
        for r in items:
            print(f"{r['id']:>4}  email {r['email_id']:<5}{r['kind']:<18}{r['destination']:<19}"
                  f"{r['status']:<10}{r['reason'] or ''}")
    elif args.review_cmd == "show":
        item = app.store.get_review(args.id)
        if not item:
            sys.exit(f"Review item {args.id} not found")
        email = app.store.get_email(item["email_id"])
        print(json.dumps({"review": item, "email": email}, indent=2, default=str))
    elif args.review_cmd == "approve":
        payload = json.loads(Path(args.payload).read_text(encoding="utf-8")) if args.payload else None
        category = Category(args.category) if args.category else None
        print(json.dumps(app.reviews.approve(args.id, args.by, payload=payload, category=category,
                                             notes=args.notes), indent=2))
    elif args.review_cmd == "reject":
        print(json.dumps(app.reviews.reject(args.id, args.by, args.notes), indent=2))
    elif args.review_cmd == "claim":
        print(json.dumps(app.reviews.claim(args.id, args.by), indent=2))
    elif args.review_cmd == "dismiss":
        print(json.dumps(app.reviews.dismiss(args.id, args.by, args.reason), indent=2))
    elif args.review_cmd == "forward":
        print(json.dumps(app.reviews.forward(args.id, args.by, args.team, args.notes), indent=2))


def cmd_contacts(app, args) -> None:
    import csv

    with open(args.file, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("booking_id") or "").strip()]
    stored = app.store.replace_contacts(rows)
    app.store.audit("contacts.loaded", "cli", count=stored)
    print(f"Loaded {stored} contact(s) for {len({r['booking_id'].strip().upper() for r in rows})} booking(s).")


def cmd_subscribe(app, args) -> None:
    """Creates the Graph subscription that calls <url>/api/graph/webhook when mail arrives (expires in ~3 days)."""
    s = app.settings
    if not s.graph_webhook_secret:
        sys.exit("Set GRAPH_WEBHOOK_SECRET in .env first (any long random string)")
    s.public_base_url = args.url
    if not app.subscriptions.enabled:
        sys.exit("The webhook needs Graph credentials and a public https:// URL")
    try:
        info = app.subscriptions.ensure(force=True)
    except Exception as exc:
        sys.exit(f"FAILED: {exc}")
    print(f"Subscribed: id={info['id']} expires {info['expires']}.")
    print(f"While the GUI runs it renews this automatically, as long as PUBLIC_BASE_URL in .env is {args.url}.")


def cmd_audit(app, args) -> None:
    for a in app.store.audit_entries(args.email_id, args.limit):
        details = json.dumps(a["details"], default=str)
        if not args.full and len(details) > 140:
            details = details[:137] + "..."
        print(f"{a['ts']}  {a['event']:<22}{a['actor']:<10}email={a['email_id'] or '-':<5}{details}")


def cmd_gui(app, args) -> None:
    import threading
    import webbrowser

    import uvicorn

    from .web import create_api

    url = f"http://{args.host}:{args.port}"
    print(f"EIE GUI running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    uvicorn.run(create_api(app), host=args.host, port=args.port, log_level="warning")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eie", description="Email Intelligence Engine")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    gui = sub.add_parser("gui", help="Start the web GUI")
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--port", type=int, default=8000)
    gui.add_argument("--no-browser", action="store_true")
    gui.set_defaults(func=cmd_gui)

    check = sub.add_parser("check-mailbox", help="Test the mailbox connection without processing anything")
    check.add_argument("--limit", type=int, default=5)
    check.set_defaults(func=cmd_check_mailbox)

    sub.add_parser("run", help="Process unread emails once").set_defaults(func=cmd_run)

    poll = sub.add_parser("poll", help="Process unread emails continuously")
    poll.add_argument("--interval", type=int, default=60)
    poll.set_defaults(func=cmd_poll)

    demo = sub.add_parser("demo", help="Process emails from a local JSON file")
    demo.add_argument("file", nargs="?", default="samples/sample_emails.json")
    demo.set_defaults(func=cmd_demo)

    emails = sub.add_parser("emails", help="List processed emails")
    emails.add_argument("--limit", type=int, default=50)
    emails.set_defaults(func=cmd_emails)

    review = sub.add_parser("review", help="Work the human review queue")
    review.set_defaults(func=cmd_review)
    rsub = review.add_subparsers(dest="review_cmd", required=True)
    rlist = rsub.add_parser("list")
    rlist.add_argument("--status", default="pending", choices=["pending", "approved", "rejected", "resolved", "dismissed", "all"])
    rlist.add_argument("--kind", choices=["review_form", "booking_approval", "ccp_review", "possible_split"])
    rsub.add_parser("show").add_argument("id", type=int)
    approve = rsub.add_parser("approve")
    approve.add_argument("id", type=int)
    approve.add_argument("--by", required=True, help="Reviewer name")
    approve.add_argument("--category", choices=[c.value for c in Category],
                         help="Set/override the category (required to route CCP items)")
    approve.add_argument("--payload", help="JSON file with the edited form data")
    approve.add_argument("--notes", default="")
    reject = rsub.add_parser("reject")
    reject.add_argument("id", type=int)
    reject.add_argument("--by", required=True)
    reject.add_argument("--notes", default="")

    for name, extra in (("claim", []), ("dismiss", ["--reason"]), ("forward", ["--team"])):
        sp = rsub.add_parser(name)
        sp.add_argument("id", type=int)
        sp.add_argument("--by", required=True)
        for flag in extra:
            sp.add_argument(flag, required=True)
        if name == "forward":
            sp.add_argument("--notes", default="")

    contacts = sub.add_parser("contacts", help="Load booking contacts for requester verification")
    contacts.add_argument("file")
    contacts.set_defaults(func=cmd_contacts)

    subscribe = sub.add_parser("subscribe", help="Create the Microsoft Graph webhook subscription")
    subscribe.add_argument("--url", required=True, help="Public base URL of this server, e.g. https://eie.example.com")
    subscribe.set_defaults(func=cmd_subscribe)

    audit = sub.add_parser("audit", help="Show the audit trail")
    audit.add_argument("--email-id", type=int)
    audit.add_argument("--limit", type=int, default=100)
    audit.add_argument("--full", action="store_true", help="Don't truncate details")
    audit.set_defaults(func=cmd_audit)
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app()
    try:
        args.func(app, args)
    except ReviewError as exc:
        sys.exit(str(exc))
    except KeyboardInterrupt:
        pass
    finally:
        app.store.close()
