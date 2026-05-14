from opcua import Client, ua

def main():
    # 1. Define the CODESYS server URL (CODESYS default is usually port 4840)
    url = "opc.tcp://127.0.0.1:1217"

    # Initialize the freeopcua Client
    client = Client(url)

    try:
        print(f"Connecting to {url} ...")
        client.connect()
        print("Successfully connected to the CODESYS project!")

 

    except Exception as e:
        print(f"An error occurred: {e}")

    finally:
        # Cleanly disconnect when finished
        print("Disconnecting...")
        try:
            client.disconnect()
        except Exception:
            pass

if __name__ == "__main__":
    main()