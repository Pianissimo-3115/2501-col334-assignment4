import socket
import sys
import struct
import time
import os

# --- Constants from Readme ---
HEADER_SIZE = 20  # 4-byte seq_num + 16-byte reserved
PACKET_SIZE = 1200
OUTFILE = "received_data.txt"
CONNECTION_TIMEOUT = 2.0 # 2-second timeout for retries
CONNECTION_RETRIES = 5   # Retry up to 5 times

def main():
    if len(sys.argv) != 3:
        print(f"Usage: python3 {sys.argv[0]} <server_ip> <server_port>")
        sys.exit(1)

    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    server_addr = (server_ip, server_port)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(CONNECTION_TIMEOUT)

    # --- Connection Setup: 1-byte message, 5 retries ---
    request_packet = b'\x01' # A single byte
    first_packet = None
    
    for i in range(CONNECTION_RETRIES):
        try:
            print(f"Sending file request to {server_addr} (Attempt {i+1}/{CONNECTION_RETRIES})")
            sock.sendto(request_packet, server_addr)
            
            # Wait for the *first data packet*
            first_packet, _ = sock.recvfrom(PACKET_SIZE)
            print("Received first packet from server. Connection established.")
            break # Success
        except socket.timeout:
            print("Connection request timed out.")
            
    if not first_packet:
        print(f"Server is unresponsive after {CONNECTION_RETRIES} attempts. Terminating.")
        sock.close()
        sys.exit(1)

    # --- Main Receive Loop ---
    expected_seq_num = 0
    
    try:
        with open(OUTFILE, 'wb') as f:
            # Process the first packet we already received
            packet_to_process = first_packet
            
            while True:
                try:
                    # Process the packet (either the first one or a new one)
                    if packet_to_process:
                        header = packet_to_process[:HEADER_SIZE]
                        payload = packet_to_process[HEADER_SIZE:]
                        seq_num, = struct.unpack('!I', header[:4]) # Unpack 4 bytes
                        
                        # Clear the packet buffer
                        packet_to_process = None
                        
                        # Check for EOF
                        if payload == b"EOF":
                            print(f"Received EOF packet {seq_num}. Download complete.")
                            # ACK the EOF packet
                            # The next "expected" packet would be seq_num + 1
                            ack_packet = struct.pack('!I 16x', seq_num + 1)
                            for _ in range(5): # Send final ACK multiple times
                                sock.sendto(ack_packet, server_addr)
                            break # Terminate client
                        
                        # Check for in-order DATA
                        if seq_num == expected_seq_num:
                            # print(f"Received in-order packet {seq_num}")
                            f.write(payload)
                            expected_seq_num += 1
                        # else:
                            # print(f"Received out-of-order {seq_num}. Expecting {expected_seq_num}. Discarding.")
                        
                        # Send cumulative ACK for the *next* packet we expect
                        ack_packet = struct.pack('!I 16x', expected_seq_num)
                        sock.sendto(ack_packet, server_addr)

                    # Wait for the next packet
                    packet_to_process, _ = sock.recvfrom(PACKET_SIZE)
                    
                except socket.timeout:
                    print(f"Client timeout: No response. Resending ACK for {expected_seq_num}")
                    # Resend last cumulative ACK
                    ack_packet = struct.pack('!I 16x', expected_seq_num)
                    sock.sendto(ack_packet, server_addr)

    except IOError as e:
        print(f"Error: Could not open output file {OUTFILE}. Error: {e}")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        print(f"File written to {OUTFILE}. Client shutting down.")
        sock.close()

if __name__ == "__main__":
    main()

