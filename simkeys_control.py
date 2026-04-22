import argparse
import os
import queue
import threading
import time

import simKeys_Client as simkeys
import simkeys_runtime as runtime


def default_dll_path():
    root = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, "SimKeysHook2", "Release", "SimKeysHook2.dll")


def print_clients(records):
    if not records:
        print("No nwmain.exe clients are running.")
        return
    for record in records:
        print(runtime.format_client_line(record))


def discover_records(args):
    return runtime.discover_clients(process_name=f"{args.process_name}.exe" if not args.process_name.lower().endswith(".exe") else args.process_name)


def cmd_list(args):
    print_clients(discover_records(args))
    return 0


def cmd_inject_next(args):
    records = discover_records(args)
    if not records:
        print("No nwmain.exe clients are running.")
        return 1
    target = runtime.find_uninjected_client(records, skip=max(args.skip, 0))
    if target is None:
        print("All discovered NWN clients are already injected.")
        return 0

    base, func = runtime.inject_client(target, os.path.abspath(args.dll), args.export)
    print(f"Injected client #{target.ordinal} pid={target.pid} base=0x{base:08X} init=0x{func:08X}")
    return 0


def cmd_inject_all(args):
    records = discover_records(args)
    if not records:
        print("No nwmain.exe clients are running.")
        return 1
    targets = [record for record in records if not record.injected]
    if not targets:
        print("All discovered NWN clients are already injected.")
        return 0

    for record in targets:
        base, func = runtime.inject_client(record, os.path.abspath(args.dll), args.export)
        print(f"Injected client #{record.ordinal} pid={record.pid} base=0x{base:08X} init=0x{func:08X}")
    return 0


def resolve_selected_client(args, require_injected=True):
    records = discover_records(args)
    record = runtime.resolve_client_selector(records, args.client, require_injected=require_injected)
    return records, record


def cmd_query(args):
    _, record = resolve_selected_client(args, require_injected=True)
    pipe = simkeys.Pipe(record.pid)
    try:
        result = simkeys.query_state(pipe)
        print(
            f"client=#{record.ordinal} pid={record.pid} "
            f"name={result['character_name'] or '<unknown>'} "
            f"player=0x{result['player_object']:08X} "
            f"installed={result['installed']} logLevel={result['log_level']}"
        )
        print(
            f"quickbar: this=0x{result['quickbar_this']:08X} page={result['quickbar_page']} "
            f"slot={result['quickbar_slot']} type={result['quickbar_slot_type']} "
            f"calls={result['quickbar_calls']}"
        )
        print(
            f"last: vk=0x{result['last_vk']:08X} rc={result['last_rc']} err={result['last_error']} "
            f"identityErr={result['identity_error']} refreshes={result['identity_refresh_count']}"
        )
    finally:
        pipe.close()
    return 0


def cmd_slot(args):
    _, record = resolve_selected_client(args, require_injected=True)
    pipe = simkeys.Pipe(record.pid)
    try:
        if args.page == 0:
            simkeys.cmd_slot(pipe, args.slot)
        else:
            simkeys.cmd_slot_page(pipe, args.page, args.slot)
    finally:
        pipe.close()
    return 0


def cmd_chat_send(args):
    _, record = resolve_selected_client(args, require_injected=True)
    pipe = simkeys.Pipe(record.pid)
    try:
        result = simkeys.chat_send(pipe, args.text, args.mode)
        print(
            f"client=#{record.ordinal} pid={record.pid} "
            f"chat-send success={result['success']} mode={result['mode']} "
            f"rc={result['rc']} err={result['err']}"
        )
    finally:
        pipe.close()
    return 0


def watch_worker(record, args, out_queue, stop_event):
    label = f"#{record.ordinal} {record.display_name}"
    try:
        pipe = simkeys.Pipe(record.pid)
    except Exception as exc:
        out_queue.put(("error", f"[{label}] could not open pipe: {exc}"))
        return

    try:
        baseline = simkeys.chat_poll(pipe, after=0, max_lines=1)
        after = 0 if args.include_backlog else baseline["latest_seq"]
        out_queue.put(("info", f"[{label}] monitoring pid={record.pid} after={after}"))
        lowered_filter = args.filter.lower() if args.filter else None
        while not stop_event.is_set():
            polled = simkeys.chat_poll(pipe, after=after, max_lines=args.max_lines)
            after = polled["latest_seq"]
            for line in polled["lines"]:
                text = line["text"]
                if lowered_filter and lowered_filter not in text.lower():
                    continue
                out_queue.put(("line", f"[{label}][{line['seq']}] {text}"))
            stop_event.wait(max(args.poll_interval, 0.01))
    except Exception as exc:
        out_queue.put(("error", f"[{label}] polling failed: {exc}"))
    finally:
        pipe.close()


def cmd_watch_chat(args):
    records = discover_records(args)
    if args.all:
        targets = runtime.find_injected_clients(records)
    else:
        targets = [runtime.resolve_client_selector(records, args.client, require_injected=True)]

    if not targets:
        print("No injected NWN clients are available for chat monitoring.")
        return 1

    out_queue = queue.Queue()
    stop_event = threading.Event()
    threads = []
    for record in targets:
        thread = threading.Thread(target=watch_worker, args=(record, args, out_queue, stop_event), daemon=True)
        thread.start()
        threads.append(thread)

    try:
        while any(thread.is_alive() for thread in threads):
            try:
                kind, message = out_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            print(message)
    except KeyboardInterrupt:
        stop_event.set()
        print("Stopping chat watch...")
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=1.0)
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description="Central SimKeys controller for multi-client NWN injection and control.")
    ap.add_argument("--process-name", default="nwmain", help="Process image name to discover. Default: nwmain")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List discovered NWN clients and whether each is injected.")

    s1 = sub.add_parser("inject-next", help="Inject the next uninjected NWN client in launch order.")
    s1.add_argument("--skip", type=int, default=0, help="Skip this many uninjected clients before injecting. Default: 0")
    s1.add_argument("--dll", default=default_dll_path())
    s1.add_argument("--export", default="InitSimKeys")

    s2 = sub.add_parser("inject-all", help="Inject every uninjected NWN client.")
    s2.add_argument("--dll", default=default_dll_path())
    s2.add_argument("--export", default="InitSimKeys")

    s3 = sub.add_parser("query", help="Query one injected client by ordinal, name fragment, or pid.")
    s3.add_argument("--client", help="Client selector. Use the list ordinal, name fragment, or pid.")

    s4 = sub.add_parser("slot", help="Trigger a quickbar slot on one injected client.")
    s4.add_argument("slot", type=int, choices=range(1, 13))
    s4.add_argument("--page", type=int, default=0, choices=[0, 1, 2], help="Quickbar page: 0=base, 1=shift, 2=ctrl")
    s4.add_argument("--client", help="Client selector. Use the list ordinal, name fragment, or pid.")

    s5 = sub.add_parser("chat-send", help="Send chat text through one injected client.")
    s5.add_argument("text")
    s5.add_argument("--mode", type=int, default=2)
    s5.add_argument("--client", help="Client selector. Use the list ordinal, name fragment, or pid.")

    s6 = sub.add_parser("watch-chat", help="Monitor chat on one or all injected clients.")
    s6.add_argument("--client", help="Client selector. Omit only when exactly one injected client exists.")
    s6.add_argument("--all", action="store_true", help="Watch every injected client in parallel.")
    s6.add_argument("--filter", help="Only print lines containing this case-insensitive substring.")
    s6.add_argument("--poll-interval", type=float, default=0.20)
    s6.add_argument("--max-lines", type=int, default=40)
    s6.add_argument("--include-backlog", action="store_true")

    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()

    try:
        if args.cmd == "list":
            return cmd_list(args)
        if args.cmd == "inject-next":
            return cmd_inject_next(args)
        if args.cmd == "inject-all":
            return cmd_inject_all(args)
        if args.cmd == "query":
            return cmd_query(args)
        if args.cmd == "slot":
            return cmd_slot(args)
        if args.cmd == "chat-send":
            return cmd_chat_send(args)
        if args.cmd == "watch-chat":
            return cmd_watch_chat(args)
        ap.error(f"unknown command: {args.cmd}")
        return 2
    except Exception as exc:
        print(f"simkeys-control: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
