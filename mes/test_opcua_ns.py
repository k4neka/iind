import asyncio
from asyncua import Client
import sys

async def main():
    client = Client(url="opc.tcp://172.21.96.1:4840")
    try:
        await client.connect()
        ns_array = await client.get_namespace_array()
        print("Namespaces:")
        for idx, uri in enumerate(ns_array):
            print(f"  {idx}: {uri}")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
