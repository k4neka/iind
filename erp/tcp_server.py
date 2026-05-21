"""TCP server on port 6666 accepting JSON client orders."""
import json
import socket
import threading

from config import TCP_HOST, TCP_PORT, FINAL_PRODUCTS
from database import save_client_order


class OrderServer:
    def __init__(self, clock, on_new_order):
        self.clock = clock
        self.on_new_order = on_new_order

    def _handle_client(self, conn, addr):
        try:
            conn.settimeout(5.0)
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            if not data:
                return
            try:
                payload = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError as e:
                conn.sendall(json.dumps({"status": "error",
                                         "msg": f"invalid JSON: {e}"}).encode())
                return

            orders = payload if isinstance(payload, list) else [payload]
            accepted, rejected = 0, []

            for o in orders:
                try:
                    name = str(o["name"])
                    nif = int(o["NIF"])
                    oid = int(o["OrderID"])
                    lines_raw = o["orders"]
                except (KeyError, TypeError, ValueError) as e:
                    rejected.append(f"malformed order: {e}")
                    continue

                cur_day = self.clock.current_day()
                lines, bad = [], False
                for ln in lines_raw:
                    t = ln.get("type")
                    if t not in FINAL_PRODUCTS:
                        rejected.append(
                            f"OrderID {oid}: piece '{t}' is not a final product"
                        )
                        bad = True
                        break
                    q = int(ln["quantity"])
                    ddate_rel = int(ln["DDate"])
                    penalty = float(ln["Penalty"])
                    lines.append((t, q, cur_day + ddate_rel, penalty))

                if bad or not lines:
                    continue

                save_client_order(name, nif, oid, cur_day, lines)
                accepted += 1

            if accepted:
                self.on_new_order()

            conn.sendall(json.dumps({
                "status": "ok",
                "accepted": accepted,
                "rejected": rejected,
            }).encode())
            print(f"[tcp] {addr} -> accepted={accepted} rejected={len(rejected)}")
        except Exception as e:
            print(f"[tcp] error with {addr}: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _run(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((TCP_HOST, TCP_PORT))
        s.listen(8)
        print(f"[tcp] ERP listening on {TCP_HOST}:{TCP_PORT}")
        while True:
            conn, addr = s.accept()
            threading.Thread(target=self._handle_client,
                             args=(conn, addr), daemon=True).start()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()