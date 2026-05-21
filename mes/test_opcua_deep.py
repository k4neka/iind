import asyncio
from asyncua import Client
import sys

async def browse_recursive(node, level=0):
    if level > 3: return
    try:
        children = await node.get_children()
        for child in children:
            name = (await child.read_browse_name()).Name
            print("  " * level + "- " + name)
            node_id = str(child.nodeid)
            if "GVL" in node_id or "GVL" in name:
                print("  " * level + "  *** FOUND GVL NODE ID: " + node_id)
            await browse_recursive(child, level + 1)
    except Exception:
        pass

async def main():
    url = "opc.tcp://172.21.96.1:4840"
    client = Client(url=url)
    try:
        await client.connect()
        await browse_recursive(client.nodes.objects)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
