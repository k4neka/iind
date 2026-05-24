"""MES entry point. Wires PLC + MQTT + DB + dispatcher + poller."""
import asyncio
import time

from config import DISPATCH_INTERVAL
from database import init_db
from opcua_client import PLCClient
from mqtt_client import MESMqtt
from poller import Poller
from cost_tracker import CostTracker
from dispatcher import Dispatcher


async def main_async():
    init_db()

    plc = PLCClient()
    if not await plc.connect():
        print("[mes] CRITICAL: cannot connect to PLC. Exiting.")
        return

    loop = asyncio.get_running_loop()

    mqtt_bridge = MESMqtt(loop=loop, plc=plc)
    cost = CostTracker(mqtt_publish_status=mqtt_bridge.publish_status)

    def on_warehouse_update(w1, w2):
        # PLC counter (unreliable for W1 but kept for W2 visibility).
        print(f"[wh] PLC W1={w1}/32  W2={w2}/32")

    poller = Poller(plc, cost, on_warehouse_update=on_warehouse_update)
    dispatcher = Dispatcher(plc, mqtt_bridge)

    mqtt_bridge.start()

    async def dispatcher_loop():
        # Aggressive 0.5 s tick: starts production as soon as one raw
        # piece is available in the locally-tracked W1.
        while True:
            try:
                await dispatcher.tick()
            except Exception as e:
                print(f"[disp] tick error: {e}")
            await asyncio.sleep(DISPATCH_INTERVAL)

    async def cost_sweep_loop():
        while True:
            cost.sweep_stuck(time.time())
            await asyncio.sleep(30)

    async def loader_ack_loop():
        while True:
            try:
                status = await plc.read_loader_status()
                if status == 3:
                    print("[mes] WARN: loader reported ERROR (status=3)")
                    await plc.clear_loader_exec()
            except Exception as e:
                print(f"[mes] loader_ack_loop error: {e}")
            await asyncio.sleep(0.5)

    async def loader_flush_loop():
        while True:
            try:
                await mqtt_bridge.flush_loader()
            except Exception as e:
                print(f"[mes] loader_flush_loop error: {e}")
            await asyncio.sleep(1.0)

    try:
        await asyncio.gather(
            poller.reg_loop(),
            poller.warehouse_loop(),
            dispatcher_loop(),
            cost_sweep_loop(),
            loader_ack_loop(),
            loader_flush_loop(),
        )
    finally:
        await plc.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n[mes] shutting down.")