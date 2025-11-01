#!/usr/bin/env python3
import socket
import sys
import time
import struct

# Constants
HEADER_SIZE = 20
MAX_PACKET_SIZE = 1200
MSS = 1180

class ReliableUDPClient:
    def __init__(self, server_ip, server_port):
        self.server_ip = server_ip
        self.server_port = int(server_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(2.0)  # 2 second timeout for initial request
        
        # Receive buffer
        self.received_data = {}  # seq_num -> data
        self.next_expected = 0   # Next expected sequence number
        self.eof_received = False
        self.eof_seq = None
        
    def create_ack(self, cum_ack, sack_blocks=None):
        """Create ACK packet with cumulative ACK and optional SACK blocks"""
        # Header: 4 bytes cumulative ACK + 16 bytes for SACK
        header = struct.pack('!I', cum_ack)
        
        # Add SACK blocks (up to 4 blocks, each 4 bytes: 2 bytes offset, 2 bytes length)
        sack_data = b''
        if sack_blocks:
            for start_offset, length in sack_blocks[:4]:  # Max 4 SACK blocks
                sack_data += struct.pack('!HH', start_offset, length)
        
        # Pad to 16 bytes
        sack_data = sack_data.ljust(16, b'\x00')
        
        return header + sack_data
    
    def parse_packet(self, packet):
        """Parse received packet to extract sequence number and data"""
        if len(packet) < HEADER_SIZE:
            return None, None
        
        seq_num = struct.unpack('!I', packet[:4])[0]
        data = packet[HEADER_SIZE:]
        
        return seq_num, data
    
    def compute_sack_blocks(self):
        """Compute SACK blocks based on received out-of-order packets"""
        sack_blocks = []
        
        if not self.received_data:
            return sack_blocks
        
        # Find contiguous blocks of received data after next_expected
        # Only include data packets (before EOF)
        sorted_seqs = sorted([seq for seq in self.received_data.keys() 
                             if seq >= self.next_expected 
                             and (self.eof_seq is None or seq < self.eof_seq)])
        
        if not sorted_seqs or sorted_seqs[0] == self.next_expected:
            # No out-of-order packets
            return sack_blocks
        
        # Build SACK blocks
        block_start = sorted_seqs[0]
        block_end = sorted_seqs[0]
        
        for seq in sorted_seqs[1:]:
            if seq == block_end + 1:
                block_end = seq
            else:
                # End current block, start new one
                start_offset = block_start - self.next_expected
                length = block_end - block_start + 1
                if start_offset >= 0 and length > 0:
                    sack_blocks.append((start_offset, length))
                
                block_start = seq
                block_end = seq
        
        # Add last block
        start_offset = block_start - self.next_expected
        length = block_end - block_start + 1
        if start_offset >= 0 and length > 0:
            sack_blocks.append((start_offset, length))
        
        return sack_blocks
    
    def send_request(self):
        """Send file request to server with retries"""
        max_retries = 5
        for attempt in range(max_retries):
            try:
                print(f"Sending request to server (attempt {attempt + 1}/{max_retries})")
                self.sock.sendto(b'\x01', (self.server_ip, self.server_port))
                
                # Wait for first packet
                packet, _ = self.sock.recvfrom(MAX_PACKET_SIZE)
                
                # Successfully received response
                print("Request successful, starting file transfer")
                return packet
                
            except socket.timeout:
                if attempt < max_retries - 1:
                    print(f"Timeout, retrying...")
                    continue
                else:
                    print("Failed to connect to server after 5 attempts")
                    return None
        
        return None
    
    def receive_file(self, output_filename):
        """Receive file from server"""
        print(f"Receiving file, will save to {output_filename}")
        
        # Send initial request and get first packet
        first_packet = self.send_request()
        if first_packet is None:
            return False
        
        # Process first packet
        seq_num, data = self.parse_packet(first_packet)
        if seq_num is not None:
            if data == b'EOF':
                self.eof_received = True
                self.eof_seq = seq_num
            else:
                self.received_data[seq_num] = data
            
            # Update next_expected
            if seq_num == self.next_expected and data != b'EOF':
                self.next_expected += 1
        
        # Send ACK for first packet
        sack_blocks = self.compute_sack_blocks()
        ack = self.create_ack(self.next_expected, sack_blocks)
        self.sock.sendto(ack, (self.server_ip, self.server_port))
        
        # [FIX] Increase timeout to handle jitter - use 300ms instead of 100ms
        # With 100ms jitter, packets can take up to 120ms+, so 300ms is safer
        self.sock.settimeout(0.3)  # 300ms timeout (was 0.1)
        
        last_ack_time = time.time()
        last_packet_time = time.time()
        ack_interval = 0.05  # [FIX] Increase ACK interval to reduce overhead (was 0.02)
        
        consecutive_timeouts = 0
        # [FIX] Reduce max timeouts but with longer timeout per iteration
        # 100 timeouts * 300ms = 30 seconds total (vs 200 * 100ms = 20 seconds before)
        max_consecutive_timeouts = 100  # Reduced from 200
        packets_since_last_check = 0
        
        # [FIX] Track when we last saw progress to detect true completion
        last_progress_time = time.time()
        last_received_count = 0
        
        while True:
            # Check for completion every 10 packets or on timeout
            if packets_since_last_check >= 10 or consecutive_timeouts > 0:
                if self.eof_received and self.eof_seq is not None:
                    # Check if we have all packets from 0 to eof_seq-1
                    all_received = all(seq in self.received_data for seq in range(self.eof_seq))
                    
                    # [FIX] Also check if we've stopped making progress
                    current_received = len(self.received_data)
                    if current_received > last_received_count:
                        last_progress_time = time.time()
                        last_received_count = current_received
                    
                    # Exit if we have all data OR if no progress for 3 seconds after EOF
                    time_since_progress = time.time() - last_progress_time
                    if all_received or (time_since_progress > 3.0):
                        if all_received:
                            print(f"All data received successfully")
                        else:
                            print(f"No progress for {time_since_progress:.1f}s after EOF, exiting")
                        
                        # Send a few final ACKs and exit
                        for _ in range(5):
                            sack_blocks = self.compute_sack_blocks()
                            ack = self.create_ack(self.next_expected, sack_blocks)
                            self.sock.sendto(ack, (self.server_ip, self.server_port))
                            time.sleep(0.02)
                        break
                packets_since_last_check = 0
            
            try:
                packet, _ = self.sock.recvfrom(MAX_PACKET_SIZE)
                consecutive_timeouts = 0  # Reset timeout counter
                packets_since_last_check += 1
                last_packet_time = time.time()
                last_progress_time = time.time()  # [FIX] Update progress time
                
                seq_num, data = self.parse_packet(packet)
                
                if seq_num is None:
                    continue
                
                # Check for EOF
                if data == b'EOF':
                    self.eof_received = True
                    self.eof_seq = seq_num
                    print(f"EOF received at sequence {seq_num}")
                    # Don't store EOF in received_data
                else:
                    # Store data if not duplicate
                    if seq_num >= self.next_expected and seq_num not in self.received_data:
                        self.received_data[seq_num] = data
                
                # Update next_expected if we can
                while self.next_expected in self.received_data:
                    self.next_expected += 1
                
                # Send ACK with SACK blocks
                sack_blocks = self.compute_sack_blocks()
                ack = self.create_ack(self.next_expected, sack_blocks)
                self.sock.sendto(ack, (self.server_ip, self.server_port))
                last_ack_time = time.time()
                
            except socket.timeout:
                consecutive_timeouts += 1
                
                # Send periodic ACKs even without receiving new data
                current_time = time.time()
                if current_time - last_ack_time > ack_interval:
                    sack_blocks = self.compute_sack_blocks()
                    ack = self.create_ack(self.next_expected, sack_blocks)
                    self.sock.sendto(ack, (self.server_ip, self.server_port))
                    last_ack_time = current_time
                
                # Check if we should exit due to timeout
                if consecutive_timeouts >= max_consecutive_timeouts:
                    if self.eof_received and self.eof_seq is not None:
                        # Check if we have all data
                        all_received = all(seq in self.received_data for seq in range(self.eof_seq))
                        if all_received:
                            print(f"All data received, exiting after {consecutive_timeouts} timeouts")
                            break
                        else:
                            missing = [seq for seq in range(self.eof_seq) if seq not in self.received_data]
                            print(f"ERROR: Missing {len(missing)} packets after timeout: {missing[:10]}")
                    else:
                        print(f"ERROR: No EOF received after {consecutive_timeouts} timeouts")
                    break
        
        # Write received data to file in order
        data_sequences = sorted([s for s in self.received_data.keys() if self.eof_seq is None or s < self.eof_seq])
        
        print(f"DEBUG: EOF seq = {self.eof_seq}")
        print(f"DEBUG: Total packets in received_data = {len(self.received_data)}")
        print(f"DEBUG: Data packets to write = {len(data_sequences)}")
        
        if data_sequences:
            print(f"DEBUG: Sequence range: {min(data_sequences)} to {max(data_sequences)}")
            # Check for gaps from 0 to EOF
            if self.eof_seq is not None:
                expected_seqs = set(range(self.eof_seq))
                received_seqs = set(data_sequences)
                missing_seqs = expected_seqs - received_seqs
                if missing_seqs:
                    print(f"DEBUG: WARNING - Missing {len(missing_seqs)} sequences: {sorted(list(missing_seqs))[:20]}")
                else:
                    print(f"DEBUG: Complete! All sequences from 0 to {self.eof_seq-1} received")
        
        total_bytes = 0
        with open(output_filename, 'wb') as f:
            for seq in data_sequences:
                data = self.received_data[seq]
                f.write(data)
                total_bytes += len(data)
        
        print(f"File transfer complete. Received {len(data_sequences)} data packets ({total_bytes} bytes).")
        print(f"File saved to {output_filename}")
        
        # Return success only if we have all expected data
        if self.eof_seq is not None:
            expected_count = self.eof_seq
            if len(data_sequences) == expected_count:
                print("✓ Transfer verified complete")
                return True
            else:
                print(f"✗ Transfer incomplete: expected {expected_count}, got {len(data_sequences)}")
                return False
        
        return True
    
    def run(self):
        """Main client loop"""
        output_filename = getattr(self, 'output_file', 'received_data.txt')
        try:
            success = self.receive_file(output_filename)
            if not success:
                print("File transfer failed")
                sys.exit(1)
        except KeyboardInterrupt:
            print("\nClient interrupted")
        finally:
            self.sock.close()

def main():
    if len(sys.argv) < 3:
        print("Usage: python3 p1_client.py <SERVER_IP> <SERVER_PORT> [OUTPUT_FILE]")
        sys.exit(1)
    
    server_ip = sys.argv[1]
    server_port = sys.argv[2]
    output_file = sys.argv[3] if len(sys.argv) > 3 else 'received_data.txt'
    
    client = ReliableUDPClient(server_ip, server_port)
    client.output_file = output_file
    client.run()

if __name__ == "__main__":
    main()