"""Client Order GUI — simulates customers sending JSON orders to the ERP.

Sends a JSON payload over TCP to the ERP (default localhost:6666).
Payload structure follows the PDF spec:

{
  "name": <string>,
  "NIF":  <number>,
  "OrderID": <number>,
  "orders": [
    {"type": "...", "quantity": N, "DDate": N, "Penalty": N},
    ...
  ]
}
"""
import json
import socket
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

FINAL_PRODUCTS = ["RWW", "SWW", "RWM", "SWM", "RMM", "SMM"]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6666

DEFAULT_CLIENT_NAME = "Fools'n Horses"
DEFAULT_NIF = 123456789
DEFAULT_ORDER_ID = 1001

DEFAULT_ORDER_LINES = [
    # (piece_type, quantity, DDate, Penalty €)
    ("RWW", 5, 2, 10),
    ("SMM", 3, 4, 15),
]


class OrderLineRow:
    """One editable row in the orders table."""

    def __init__(self, parent, row, piece="RWW", qty=1, ddate=2, penalty=10,
                 on_delete=None):
        self.frame = ttk.Frame(parent)
        self.frame.grid(row=row, column=0, sticky="ew", pady=2)

        self.piece_var   = tk.StringVar(value=piece)
        self.qty_var     = tk.StringVar(value=str(qty))
        self.ddate_var   = tk.StringVar(value=str(ddate))
        self.penalty_var = tk.StringVar(value=str(penalty))

        ttk.Combobox(self.frame, textvariable=self.piece_var,
                     values=FINAL_PRODUCTS, width=8,
                     state="readonly").grid(row=0, column=0, padx=4)
        ttk.Entry(self.frame, textvariable=self.qty_var,
                  width=8).grid(row=0, column=1, padx=4)
        ttk.Entry(self.frame, textvariable=self.ddate_var,
                  width=8).grid(row=0, column=2, padx=4)
        ttk.Entry(self.frame, textvariable=self.penalty_var,
                  width=10).grid(row=0, column=3, padx=4)

        ttk.Button(self.frame, text="✕", width=3,
                   command=lambda: on_delete(self) if on_delete else None
                   ).grid(row=0, column=4, padx=4)

    def to_dict(self):
        return {
            "type":     self.piece_var.get().strip(),
            "quantity": int(self.qty_var.get()),
            "DDate":    int(self.ddate_var.get()),
            "Penalty":  float(self.penalty_var.get()),
        }

    def destroy(self):
        self.frame.destroy()


class ClientGUI:
    def __init__(self, root):
        self.root = root
        root.title("ERP Client Order Simulator")
        root.geometry("760x640")

        self.rows: list[OrderLineRow] = []

        self._build_header(root)
        self._build_orders_section(root)
        self._build_actions(root)
        self._build_log(root)

        # Seed default rows
        for piece, qty, ddate, penalty in DEFAULT_ORDER_LINES:
            self._add_row(piece, qty, ddate, penalty)

    # ---------- UI ----------
    def _build_header(self, parent):
        frm = ttk.LabelFrame(parent, text="Connection & Client")
        frm.pack(fill="x", padx=10, pady=8)

        # Host / port
        ttk.Label(frm, text="ERP Host:").grid(row=0, column=0, sticky="e", padx=4, pady=3)
        self.host_var = tk.StringVar(value=DEFAULT_HOST)
        ttk.Entry(frm, textvariable=self.host_var, width=18).grid(row=0, column=1, sticky="w")

        ttk.Label(frm, text="Port:").grid(row=0, column=2, sticky="e", padx=4)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        ttk.Entry(frm, textvariable=self.port_var, width=8).grid(row=0, column=3, sticky="w")

        # Client identity
        ttk.Label(frm, text="Client Name:").grid(row=1, column=0, sticky="e", padx=4, pady=3)
        self.name_var = tk.StringVar(value=DEFAULT_CLIENT_NAME)
        ttk.Entry(frm, textvariable=self.name_var, width=22).grid(row=1, column=1, sticky="w")

        ttk.Label(frm, text="NIF:").grid(row=1, column=2, sticky="e", padx=4)
        self.nif_var = tk.StringVar(value=str(DEFAULT_NIF))
        ttk.Entry(frm, textvariable=self.nif_var, width=14).grid(row=1, column=3, sticky="w")

        ttk.Label(frm, text="OrderID:").grid(row=2, column=0, sticky="e", padx=4, pady=3)
        self.order_id_var = tk.StringVar(value=str(DEFAULT_ORDER_ID))
        ttk.Entry(frm, textvariable=self.order_id_var, width=14).grid(row=2, column=1, sticky="w")

    def _build_orders_section(self, parent):
        frm = ttk.LabelFrame(parent, text="Order Lines")
        frm.pack(fill="both", expand=False, padx=10, pady=8)

        header = ttk.Frame(frm)
        header.pack(fill="x")
        for i, (text, w) in enumerate([
            ("Piece Type", 10), ("Quantity", 10),
            ("DDate (days)", 12), ("Penalty (€/day)", 14), ("", 4),
        ]):
            ttk.Label(header, text=text, width=w, anchor="center"
                      ).grid(row=0, column=i, padx=4)

        self.rows_container = ttk.Frame(frm)
        self.rows_container.pack(fill="x", pady=4)

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="+ Add line",
                   command=lambda: self._add_row()).pack(side="left", padx=4)
        ttk.Button(btns, text="Reset defaults",
                   command=self._reset_defaults).pack(side="left", padx=4)

    def _build_actions(self, parent):
        frm = ttk.Frame(parent)
        frm.pack(fill="x", padx=10, pady=4)

        ttk.Button(frm, text="Preview JSON",
                   command=self.preview_json).pack(side="left", padx=4)
        ttk.Button(frm, text="Send Order to ERP",
                   command=self.send_order).pack(side="left", padx=4)
        ttk.Button(frm, text="Clear log",
                   command=lambda: self.log_widget.delete("1.0", "end")
                   ).pack(side="right", padx=4)

    def _build_log(self, parent):
        frm = ttk.LabelFrame(parent, text="Log / ERP Responses")
        frm.pack(fill="both", expand=True, padx=10, pady=8)
        self.log_widget = scrolledtext.ScrolledText(
            frm, height=14, wrap="word", font=("Consolas", 10)
        )
        self.log_widget.pack(fill="both", expand=True, padx=4, pady=4)

    # ---------- Row management ----------
    def _add_row(self, piece="RWW", qty=1, ddate=2, penalty=10):
        row = OrderLineRow(
            self.rows_container, len(self.rows),
            piece=piece, qty=qty, ddate=ddate, penalty=penalty,
            on_delete=self._delete_row,
        )
        self.rows.append(row)

    def _delete_row(self, row: OrderLineRow):
        if row in self.rows:
            self.rows.remove(row)
            row.destroy()

    def _reset_defaults(self):
        for r in list(self.rows):
            self._delete_row(r)
        for piece, qty, ddate, penalty in DEFAULT_ORDER_LINES:
            self._add_row(piece, qty, ddate, penalty)
        self.name_var.set(DEFAULT_CLIENT_NAME)
        self.nif_var.set(str(DEFAULT_NIF))
        self.order_id_var.set(str(DEFAULT_ORDER_ID))

    # ---------- Order building ----------
    def _build_payload(self):
        if not self.rows:
            raise ValueError("At least one order line is required.")
        try:
            payload = {
                "name":    self.name_var.get().strip(),
                "NIF":     int(self.nif_var.get()),
                "OrderID": int(self.order_id_var.get()),
                "orders":  [r.to_dict() for r in self.rows],
            }
        except ValueError as e:
            raise ValueError(f"Invalid numeric field: {e}")

        if not payload["name"]:
            raise ValueError("Client name cannot be empty.")
        for line in payload["orders"]:
            if line["type"] not in FINAL_PRODUCTS:
                raise ValueError(f"'{line['type']}' is not a final product.")
            if line["quantity"] <= 0:
                raise ValueError("Quantity must be > 0.")
            if line["DDate"] < 0:
                raise ValueError("DDate cannot be negative.")
            if line["Penalty"] < 0:
                raise ValueError("Penalty cannot be negative.")
        return payload

    # ---------- Actions ----------
    def preview_json(self):
        try:
            payload = self._build_payload()
        except Exception as e:
            messagebox.showerror("Invalid order", str(e))
            return
        self._log("--- Preview JSON ---")
        self._log(json.dumps(payload, indent=2))

    def send_order(self):
        try:
            payload = self._build_payload()
            host = self.host_var.get().strip()
            port = int(self.port_var.get())
        except Exception as e:
            messagebox.showerror("Invalid order", str(e))
            return

        threading.Thread(target=self._send_thread,
                         args=(payload, host, port),
                         daemon=True).start()

    def _send_thread(self, payload, host, port):
        self._log(f"[send] {host}:{port} <- OrderID={payload['OrderID']} "
                  f"({len(payload['orders'])} line(s))")
        try:
            with socket.create_connection((host, port), timeout=5) as s:
                s.sendall(json.dumps(payload).encode("utf-8"))
                s.shutdown(socket.SHUT_WR)
                resp = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
            text = resp.decode("utf-8", errors="replace").strip()
            self._log(f"[recv] {text or '(no response)'}")
        except Exception as e:
            self._log(f"[error] {e}")

    def _log(self, msg):
        self.log_widget.insert("end", msg + "\n")
        self.log_widget.see("end")


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        # Pick a modern theme if available
        for theme in ("clam", "alt", "default"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
    except Exception:
        pass
    ClientGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()