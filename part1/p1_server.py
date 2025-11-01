import socket
import sys
import struct
import time
import select
import os

# --- Constants from Readme ---
HEADER_SIZE = 20  # 4-byte seq_num + 16-byte reserved
PAYLOAD_SIZE = 1180 # 1200 - 20
PACKET_SIZE = 1200
FILENAME = "data.txt"
TIMEOUT = 0.5  # 500ms RTO

def prepare_packets(filename):
    """Reads the file, splits it into packets, and adds EOF packet."""
    packets = []
    seq_num = 0
    try:
        with open(filename, 'rb') as f:
            while True:
                payload = f.read(PAYLOAD_SIZE)
                if not payload:
                    break
                # Header: 4-byte seq_num, 16-bytes padding (16x)
                header = struct.pack('!I 16x', seq_num)
                packets.append(header + payload)
                seq_num += 1
        
        # Add the final EOF packet
        print(f"File prepared: {seq_num} data packets.")
        header = struct.pack('!I 16x', seq_num)
        packets.append(header + b"EOF")
        print("EOF packet added.")
        
    except FileNotFoundError:
        print(f"Error: File '{filename}' not found.")
        sys.exit(1)
    return packets

def main():
    if len(sys.argv) != 4:
        print(f"Usage: python3 {sys.argv[0]} <server_ip> <server_port> <window_size_bytes>")
        sys.exit(1)

    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    SWS_BYTES = int(sys.argv[3])
    
    # Calculate window size in *packets*
    SWS_PACKETS = max(1, SWS_BYTES // PAYLOAD_SIZE)
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    try:
        sock.bind(('', server_port)) # Bind to 0.0.0.0 for compatibility
    except socket.error as e:
        print(f"Socket bind failed: {e}")
        sys.exit(1)

    print(f"Server listening on {server_ip}:{server_port} (bound to 0.0.0.0) with SWS={SWS_BYTES} bytes ({SWS_PACKETS} packets)")

    # --- Wait for 1-byte file request ---
    try:
        print("Waiting for client file request...")
        request, client_addr = sock.recvfrom(1) # Wait for 1-byte message
        if not request:
            print("Received empty request. Shutting down.")
            sock.close()
            return
    except Exception as e:
        print(f"Error waiting for request: {e}")
        sock.close()
        return

    print(f"Received request from {client_addr}. Preparing file...")
    packets_to_send = prepare_packets(FILENAME)
    total_packets = len(packets_to_send)

    # --- Go-Back-N Logic ---
    base = 0
    next_seq_num = 0
    dup_ack_count = 0
    last_ack_num = -1

    while base < total_packets:
        # 1. Send all packets in the current window
        while next_seq_num < base + SWS_PACKETS and next_seq_num < total_packets:
            packet = packets_to_send[next_seq_num]
            sock.sendto(packet, client_addr)
            # print(f"Sent packet {next_seq_num}")
            next_seq_num += 1

        # 2. Wait for ACK with a timeout
        ready = select.select([sock], [], [], TIMEOUT)
        
        if not ready[0]:
            # Timeout occurred
            print(f"Timeout: Resending window from {base}")
            # Go-Back-N: Resend all packets from base to next_seq_num
            for i in range(base, next_seq_num):
                sock.sendto(packets_to_send[i], client_addr)
            # Reset duplicate ACK counter on timeout
            dup_ack_count = 0
            continue 

        # 3. Process incoming ACK
        try:
            while True:
                # Drain all waiting ACKs
                ready_to_read = select.select([sock], [], [], 0)[0]
                if not ready_to_read:
                    break
                    
                ack_packet, _ = sock.recvfrom(PACKET_SIZE)
                header = ack_packet[:HEADER_SIZE]
                
                # Unpack only the 4-byte sequence number
                ack_num, = struct.unpack('!I', header[:4])

                # --- Cumulative ACK ---
                if ack_num > base:
                    # print(f"Received good ACK {ack_num}. Window slides.")
                    base = ack_num
                    dup_ack_count = 0
                    last_ack_num = ack_num
                
                # --- Fast Retransmit ---
                elif ack_num == last_ack_num:
                    dup_ack_count += 1
                    # print(f"Received duplicate ACK {ack_num} (Count: {dup_ack_count})")
                    if dup_ack_count == 3:
                        print(f"*** Fast Retransmit: Resending packet {base} ***")
                        sock.sendto(packets_to_send[base], client_addr)
                        dup_ack_count = 0 # Reset count
                
                else:
                    # Old, ignored ACK
                    last_ack_num = ack_num
                    dup_ack_count = 0

        except Exception as e:
            pass # Ignore socket errors

    print("All packets ACKed (including EOF). Server shutting down.")
    sock.close()

if __name__ == "__main__":
    main()

