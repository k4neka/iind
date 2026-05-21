import asyncio
from asyncua import Client

async def main():
    url = "opc.tcp://172.21.96.1:4840"
    print(f"Connecting to {url}...")
    client = Client(url=url)
    try:
        await client.connect()
        print("Connected!")
        
        # Get the root node
        root = client.nodes.root
        print("\nExploring address space to find GVL variables...")
        
        objects = client.nodes.objects
        children = await objects.get_children()
        
        for child in children:
            node_class = await child.read_node_class()
            bname = await child.read_browse_name()
            print(f"- {bname.Name}")
            
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
