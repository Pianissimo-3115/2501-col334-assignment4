import socket
import sys
import struct
import time

# --- Constants ---
HEADER_SIZE = 20  # 4-byte seq_num + 16-byte reserved
PACKET_SIZE = 1200
OUTFILE = "received_data.txt"
CONNECTION_TIMEOUT = 2.0
CONNECTION_RETRIES = 5
SACK_BITMAP_BYTES = 16  # 128 bits


def build_sack_bitmap(next_expected, received_dict):
    """Build 128-bit bitmap (16 bytes) marking received packets."""
    bits = 0
    for i in range(SACK_BITMAP_BYTES * 8):
        if (next_expected + i) in received_dict:
            bits |= (1 << i)
    return bits.to_bytes(SACK_BITMAP_BYTES, byteorder='little')


def main():
    if len(sys.argv) != 3:
        print(f"Usage: python3 {sys.argv[0]} <server_ip> <server_port>")
        sys.exit(1)

    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    server_addr = (server_ip, server_port)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(CONNECTION_TIMEOUT)

    # --- Connection Setup ---
    request_packet = b'\x01'
    first_packet = None

    for i in range(CONNECTION_RETRIES):
        try:
            print(f"Sending file request to {server_addr} (Attempt {i+1}/{CONNECTION_RETRIES})")
            sock.sendto(request_packet, server_addr)
            first_packet, _ = sock.recvfrom(PACKET_SIZE)
            print("Received first packet from server. Connection established.")
            break
        except socket.timeout:
            print("Connection request timed out.")

    if not first_packet:
        print(f"Server is unresponsive after {CONNECTION_RETRIES} attempts. Terminating.")
        sock.close()
        sys.exit(1)

    # --- Main Receive Loop ---
    expected_seq_num = 0
    received = {}          # seq_num -> payload
    data_buffer = bytearray()
    eof_seq = None

    try:
        packet_to_process = first_packet

        while True:
            try:
                if packet_to_process:
                    header = packet_to_process[:HEADER_SIZE]
                    payload = packet_to_process[HEADER_SIZE:]
                    seq_num, = struct.unpack('!I', header[:4])
                    packet_to_process = None

                    if not payload:
                        # Ignore empty payloads
                        packet_to_process = None
                        continue

                    # --- Handle EOF ---
                    if payload == b"EOF":
                        if eof_seq is None:
                            eof_seq = seq_num
                            print(f"Received EOF packet seq={seq_num}. Marked EOF in buffer.")
                            received[seq_num] = b''  # mark EOF as received
                        else:
                            print(f"Duplicate EOF packet seq={seq_num} ignored.")
                    else:
                        # Normal data packet
                        if seq_num not in received:
                            received[seq_num] = payload

                    # Deliver in order up to EOF
                    while expected_seq_num in received and expected_seq_num != eof_seq:
                        chunk = received.pop(expected_seq_num)
                        data_buffer.extend(chunk)
                        expected_seq_num += 1

                    # Build SACK bitmap relative to expected_seq_num
                    bitmap = build_sack_bitmap(expected_seq_num, received)
                    ack_packet = struct.pack('!I', expected_seq_num) + bitmap
                    sock.sendto(ack_packet, server_addr)

                    # If EOF received and all data delivered, exit
                    if eof_seq is not None:
                        all_received = all(
                            seq in received or seq < expected_seq_num
                            for seq in range(0, eof_seq + 1)
                        )
                        if all_received:
                            print("All data up to EOF received and written to buffer.")
                            for _ in range(5):
                                sock.sendto(ack_packet, server_addr)
                            break

                # Wait for next packet
                packet_to_process, _ = sock.recvfrom(PACKET_SIZE)

            except socket.timeout:
                # On timeout, resend ACK with current SACK bitmap
                if eof_seq is not None:
                    print(f"Client timeout after EOF. Resent final ACK next_expected={expected_seq_num}")
                else:
                    print(f"Client timeout. Resending ACK next_expected={expected_seq_num}")

                bitmap = build_sack_bitmap(expected_seq_num, received)
                ack_packet = struct.pack('!I', expected_seq_num) + bitmap
                try:
                    if eof_seq is not None:
                        for _ in range(5):
                            sock.sendto(ack_packet, server_addr)
                    else:
                        sock.sendto(ack_packet, server_addr)
                except Exception as e:
                    print("Failed to resend ACK:", e)

    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        try:
            with open(OUTFILE, 'wb') as f:
                f.write(data_buffer)
            print(f"File written to {OUTFILE}. Size: {len(data_buffer)} bytes.")
        except Exception as e:
            print(f"Error writing output file: {e}")
        sock.close()
        print("Client shutting down.")


if __name__ == "__main__":
    main()
