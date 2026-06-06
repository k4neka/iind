import tkinter as tk
from tkinter import messagebox
from opcua import Client, ua

# --- CONFIGURAÇÕES OPC-UA ---
# Altera a porta para 4840 ou 1217, dependendo da tua configuração no CODESYS
# --- CONFIGURAÇÕES OPC-UA ---
OPC_URL = "opc.tcp://172.21.96.1:4840"
# Prefixo típico do CODESYS. Pode variar dependendo do nome da tua aplicação!
# Usa o UaExpert para copiar o NodeId exato se este der erro "BadNodeIdUnknown"
PREFIX = "ns=4;s=|var|CODESYS Control Win V3 x64.Application.GVL."

NODE_RECV_CMD = f"{PREFIX}Cell_1_Top_Order.recv_cmd"
NODE_PIECE_ID = f"{PREFIX}Cell_1_Top_Order.Workpiece.InitPiece"
NODE_FREE_CMD = f"{PREFIX}Cell_1_Top_Status.free_cmd"
# Vamos assumir que a Célula 1 regista a peça no índice 0 do array Reg (ajusta se for outro)
NODE_REG      = f"{PREFIX}Reg[0]" 

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Controlo de Produção - CODESYS")
        self.geometry("350x400")
        self.configure(padx=20, pady=20)

        self.client = None
        self.connected = False

        # Variáveis OPC-UA guardadas após ligação
        self.node_recv = None
        self.node_id = None
        self.node_free = None
        self.node_reg = None

        self.setup_ui()

    def setup_ui(self):
        # --- ESTADO DA LIGAÇÃO ---
        self.lbl_status = tk.Label(self, text="Status: Desligado", fg="red", font=("Arial", 12, "bold"))
        self.lbl_status.pack(pady=(0, 10))

        self.btn_connect = tk.Button(self, text="Ligar ao PLC", command=self.connect_plc, width=20)
        self.btn_connect.pack(pady=5)

        tk.Frame(self, height=2, bd=1, relief=tk.SUNKEN).pack(fill=tk.X, pady=10)

        # --- MONITORIZAÇÃO ---
        tk.Label(self, text="--- Estado da Célula 1 ---", font=("Arial", 10, "italic")).pack()
        
        self.lbl_free = tk.Label(self, text="Livre (free_cmd): --", font=("Arial", 11))
        self.lbl_free.pack(pady=5)

        self.lbl_reg = tk.Label(self, text="Peça na Máquina: --", font=("Arial", 11))
        self.lbl_reg.pack(pady=5)

        tk.Frame(self, height=2, bd=1, relief=tk.SUNKEN).pack(fill=tk.X, pady=10)

        # --- COMANDOS ---
        tk.Label(self, text="ID da Peça a Enviar:", font=("Arial", 10)).pack()
        self.entry_piece = tk.Entry(self, font=("Arial", 12), justify="center")
        self.entry_piece.insert(0, "101") # Valor por defeito
        self.entry_piece.pack(pady=5)

        self.btn_send = tk.Button(self, text="Enviar Ordem", command=self.send_command, state=tk.DISABLED, bg="#4CAF50", fg="white", font=("Arial", 10, "bold"))
        self.btn_send.pack(pady=10)

    def connect_plc(self):
        try:
            self.client = Client(OPC_URL)
            self.client.connect()
            
            # Mapear os nós
            self.node_recv = self.client.get_node(NODE_RECV_CMD)
            self.node_id = self.client.get_node(NODE_PIECE_ID)
            self.node_free = self.client.get_node(NODE_FREE_CMD)
            self.node_reg = self.client.get_node(NODE_REG)

            self.connected = True
            self.lbl_status.config(text="Status: Ligado", fg="green")
            self.btn_connect.config(state=tk.DISABLED)
            self.btn_send.config(state=tk.NORMAL)

            # Iniciar a leitura cíclica dos dados (Polling a cada 500ms)
            self.update_data()

        except Exception as e:
            messagebox.showerror("Erro de Ligação", f"Não foi possível ligar ao CODESYS:\n{e}")

    def update_data(self):
        if self.connected:
            try:
                # Ler valores do PLC
                is_free = self.node_free.get_value()
                piece_in_reg = self.node_reg.get_value()

                # Atualizar Interface
                self.lbl_free.config(
                    text=f"Livre (free_cmd): {'SIM (Pronta)' if is_free else 'NÃO (Ocupada)'}",
                    fg="green" if is_free else "orange"
                )
                
                self.lbl_reg.config(text=f"Peça na Máquina: {piece_in_reg if piece_in_reg > 0 else 'Vazia'}")

                # Agendar próxima atualização
                self.after(500, self.update_data)
            except Exception as e:
                print(f"Erro a ler dados: {e}")
                self.connected = False
                self.lbl_status.config(text="Status: Erro de Leitura", fg="red")

    def send_command(self):
        if not self.connected:
            return

        try:
            # 1. Obter ID da peça introduzido
            piece_id = int(self.entry_piece.get())

            # 2. Escrever o ID da Peça na estrutura (Importante usar VariantType correto: INT16)
            self.node_id.set_value(ua.DataValue(ua.Variant(piece_id, ua.VariantType.Int16)))

            # 3. Disparar a ordem (recv_cmd = TRUE) -> Variante BOOL
            self.node_recv.set_value(ua.DataValue(ua.Variant(True, ua.VariantType.Boolean)))

            print(f"Ordem enviada com sucesso: Peça {piece_id}")

        except ValueError:
            messagebox.showwarning("Erro de Input", "Por favor introduz um número inteiro válido para o ID da peça.")
        except Exception as e:
            messagebox.showerror("Erro ao Escrever", f"Falha ao enviar comando ao PLC:\n{e}")

if __name__ == "__main__":
    app = App()
    app.mainloop()