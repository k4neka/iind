from opcua import Client, ua

def main():
    
    url = "opc.tcp://127.0.0.1:4840"
    
    # 2. Initialize the freeopcua Client
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
        client.disconnect()

if __name__ == "__main__":
    main()