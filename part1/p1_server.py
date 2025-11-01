import socket
import sys
import struct
import time
import select
import os

# --- Constants ---
HEADER_SIZE = 20  # 4-byte seq_num + 16-byte reserved
PAYLOAD_SIZE = 1180
PACKET_SIZE = 1200
FILENAME = "data.txt"
TIMEOUT = 1.0
SACK_BITMAP_BYTES = 16  # 128 bits


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
                header = struct.pack('!I 16x', seq_num)
                packets.append(header + payload)
                seq_num += 1

        # Add final EOF packet
        print(f"File prepared: {seq_num} data packets.")
        header = struct.pack('!I 16x', seq_num)
        packets.append(header + b"EOF")
        print(f"EOF packet added with seq={seq_num}.")

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
    SWS_PACKETS = max(1, SWS_BYTES // PAYLOAD_SIZE)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('', server_port))

    print(f"Server listening on {server_ip}:{server_port} (bound to 0.0.0.0) with SWS={SWS_BYTES} bytes ({SWS_PACKETS} packets)")

    # --- Wait for request ---
    print("Waiting for client file request...")
    request, client_addr = sock.recvfrom(1)
    if not request:
        print("Empty request. Exiting.")
        sock.close()
        return

    print(f"Received request from {client_addr}. Preparing file...")
    packets_to_send = prepare_packets(FILENAME)
    total_packets = len(packets_to_send)
    eof_seq = total_packets - 1

    base = 0
    next_seq_num = 0
    client_reported_received = set()

    last_ack_num = -1
    dup_ack_count = 0
    eof_acked = False

    print("Starting data transmission...")

    while not eof_acked:
        # --- Send new packets within window ---
        while next_seq_num < base + SWS_PACKETS and next_seq_num < total_packets:
            sock.sendto(packets_to_send[next_seq_num], client_addr)
            next_seq_num += 1

        # --- Wait for ACK ---
        ready = select.select([sock], [], [], TIMEOUT)
        if not ready[0]:
            # Timeout → selective retransmission
            if eof_acked:
                continue
            print(f"Timeout: selective retransmit within window base={base}")
            for seq in range(base, min(base + SWS_PACKETS, total_packets)):
                if seq in client_reported_received:
                    continue
                sock.sendto(packets_to_send[seq], client_addr)
            dup_ack_count = 0
            continue

        # --- Receive ACKs ---
        try:
            while True:
                r = select.select([sock], [], [], 0)[0]
                if not r:
                    break

                ack_packet, _ = sock.recvfrom(PACKET_SIZE)
                if len(ack_packet) < HEADER_SIZE:
                    continue

                ack_next_expected, = struct.unpack('!I', ack_packet[:4])
                sack_bytes = ack_packet[4:4 + SACK_BITMAP_BYTES]
                bits = int.from_bytes(sack_bytes, byteorder='little')

                # Mark ACKed packets
                for seq in range(0, ack_next_expected):
                    client_reported_received.add(seq)
                for i in range(SACK_BITMAP_BYTES * 8):
                    if (bits >> i) & 1:
                        client_reported_received.add(ack_next_expected + i)

                # Slide base
                while base < total_packets and base in client_reported_received:
                    base += 1

                # Fast retransmit
                if ack_next_expected == last_ack_num:
                    dup_ack_count += 1
                else:
                    dup_ack_count = 1
                last_ack_num = ack_next_expected

                if dup_ack_count >= 3:
                    for seq in range(base, min(base + SWS_PACKETS, total_packets)):
                        if seq not in client_reported_received:
                            print(f"Fast retransmit of packet {seq}")
                            sock.sendto(packets_to_send[seq], client_addr)
                            break
                    dup_ack_count = 0

                # --- EOF ACK Check ---
                if eof_seq in client_reported_received or ack_next_expected > eof_seq:
                    print(f"EOF ACK received (next_expected={ack_next_expected}, eof_seq={eof_seq}).")
                    eof_acked = True
                    break

        except Exception as e:
            print(f"ACK handling error: {e}")
            continue

    print("✅ All packets ACKed including EOF. Server shutting down cleanly.")
    time.sleep(1.0)  # Let last ACKs drain
    sock.close()


if __name__ == "__main__":
    main()
