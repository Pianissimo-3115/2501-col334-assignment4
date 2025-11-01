#!/usr/bin/env python3
import socket
import sys
import time
import struct
import os

# Constants
MSS = 1180  # Maximum segment size for data
HEADER_SIZE = 20
MAX_PACKET_SIZE = 1200
INITIAL_RTO = 1.0  # Initial retransmission timeout in seconds
ALPHA = 0.125
BETA = 0.25
K = 4

class ReliableUDPServer:
    def __init__(self, server_ip, server_port, sws):
        self.server_ip = server_ip
        self.server_port = server_port
        self.sws = sws  # Sender window size in bytes
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.server_ip, self.server_port))
        
        # State variables
        self.base_seq = 0  # Oldest unacknowledged sequence number
        self.next_seq = 0  # Next sequence number to send
        self.window = {}   # Dictionary: seq_num -> (data, send_time)
        
        # RTO estimation
        self.estimated_rtt = None
        self.dev_rtt = None
        self.rto = INITIAL_RTO
        
        # Statistics
        self.duplicate_ack_count = {}
        
        # [FIX] Track which packets are known to be received (from SACK)
        self.sacked_packets = set()
        
    def estimate_rto(self, sample_rtt):
        """Update RTO using exponential weighted moving average"""
        if self.estimated_rtt is None:
            self.estimated_rtt = sample_rtt
            self.dev_rtt = sample_rtt / 2
        else:
            self.dev_rtt = (1 - BETA) * self.dev_rtt + BETA * abs(sample_rtt - self.estimated_rtt)
            self.estimated_rtt = (1 - ALPHA) * self.estimated_rtt + ALPHA * sample_rtt
        
        self.rto = self.estimated_rtt + K * self.dev_rtt
        # [FIX] Clamp RTO to wider range for jitter tolerance
        self.rto = max(0.3, min(self.rto, 3.0))  # Was 0.2-2.0, now 0.3-3.0
    
    def create_packet(self, seq_num, data):
        """Create a packet with sequence number and data"""
        # Header: 4 bytes seq_num + 16 bytes reserved (all zeros)
        header = struct.pack('!I', seq_num) + b'\x00' * 16
        return header + data
    
    def parse_ack(self, packet):
        """Parse ACK packet to extract cumulative ACK and SACK blocks"""
        if len(packet) < 4:
            return None, []
        
        cum_ack = struct.unpack('!I', packet[:4])[0]
        
        # Parse SACK blocks from reserved area (bytes 4-20)
        sack_blocks = []
        if len(packet) >= 20:
            reserved = packet[4:20]
            # Each SACK block is 4 bytes: 2 bytes start, 2 bytes length
            for i in range(0, 16, 4):
                if i + 4 <= len(reserved):
                    start_offset = struct.unpack('!H', reserved[i:i+2])[0]
                    length = struct.unpack('!H', reserved[i+2:i+4])[0]
                    if start_offset > 0 or length > 0:  # Valid SACK block
                        sack_blocks.append((start_offset, length))
        
        return cum_ack, sack_blocks
    
    def send_file(self, client_addr, filename):
        """Send file to client using sliding window with SACK"""
        if not os.path.exists(filename):
            print(f"File {filename} not found")
            return
        
        # Read file
        with open(filename, 'rb') as f:
            file_data = f.read()
        
        print(f"Sending file {filename} ({len(file_data)} bytes) to {client_addr}")
        
        # Split into chunks
        chunks = []
        for i in range(0, len(file_data), MSS):
            chunks.append(file_data[i:i+MSS])
        
        total_data_packets = len(chunks)
        
        # Add EOF marker as separate packet
        chunks.append(b'EOF')
        
        total_packets = len(chunks)
        self.sock.settimeout(0.01)  # Non-blocking with short timeout
        
        last_ack_time = time.time()
        
        while self.base_seq < total_packets:
            current_time = time.time()
            
            # Send new packets within window
            while self.next_seq < total_packets and \
                  (self.next_seq - self.base_seq) * MSS < self.sws:
                
                if self.next_seq not in self.window:
                    packet = self.create_packet(self.next_seq, chunks[self.next_seq])
                    self.sock.sendto(packet, client_addr)
                    self.window[self.next_seq] = (packet, current_time)
                    self.next_seq += 1
            
            # Check for timeout on base packet
            if self.base_seq in self.window:
                _, send_time = self.window[self.base_seq]
                if current_time - send_time > self.rto:
                    # Timeout: retransmit base packet
                    packet, _ = self.window[self.base_seq]
                    self.sock.sendto(packet, client_addr)
                    self.window[self.base_seq] = (packet, current_time)
                    # [FIX] Also retransmit other packets in window that haven't been SACKed
                    for seq in range(self.base_seq + 1, min(self.next_seq, total_packets)):
                        if seq in self.window and seq not in self.sacked_packets:
                            packet, send_time = self.window[seq]
                            if current_time - send_time > self.rto:
                                self.sock.sendto(packet, client_addr)
                                self.window[seq] = (packet, current_time)
            
            # Try to receive ACK
            try:
                ack_packet, _ = self.sock.recvfrom(1024)
                ack_time = time.time()
                cum_ack, sack_blocks = self.parse_ack(ack_packet)
                
                if cum_ack is None:
                    continue
                
                # Process cumulative ACK
                if cum_ack > self.base_seq:
                    # Calculate RTT sample for base packet
                    if self.base_seq in self.window:
                        _, send_time = self.window[self.base_seq]
                        sample_rtt = ack_time - send_time
                        self.estimate_rto(sample_rtt)
                    
                    # Remove acknowledged packets
                    for seq in range(self.base_seq, cum_ack):
                        self.window.pop(seq, None)
                        self.duplicate_ack_count.pop(seq, None)
                        self.sacked_packets.discard(seq)
                    
                    self.base_seq = cum_ack
                    last_ack_time = ack_time
                
                elif cum_ack == self.base_seq:
                    # Duplicate ACK
                    self.duplicate_ack_count[cum_ack] = self.duplicate_ack_count.get(cum_ack, 0) + 1
                    
                    # Fast retransmit after 3 duplicate ACKs
                    if self.duplicate_ack_count[cum_ack] == 3:
                        if self.base_seq in self.window:
                            packet, _ = self.window[self.base_seq]
                            self.sock.sendto(packet, client_addr)
                            self.window[self.base_seq] = (packet, current_time)
                
                # [FIX] Process SACK blocks - mark packets as received, identify holes
                if sack_blocks and self.base_seq < total_packets:
                    # First, mark all SACKed packets
                    for start_offset, length in sack_blocks:
                        if start_offset == 0 or length == 0:
                            continue
                        
                        start_seq = self.base_seq + start_offset
                        end_seq = min(start_seq + length, total_packets)
                        
                        # Validate range
                        if start_seq < self.base_seq or start_seq >= total_packets:
                            continue
                        
                        # Mark as SACKed (but don't remove from window yet)
                        for seq in range(start_seq, end_seq):
                            self.sacked_packets.add(seq)
                    
                    # Now find ALL holes in the window and retransmit them
                    # A hole is any packet in window that's NOT in sacked_packets
                    window_end = min(self.next_seq, total_packets)
                    for seq in range(self.base_seq, window_end):
                        if seq in self.window and seq not in self.sacked_packets:
                            packet, send_time = self.window[seq]
                            # [FIX] Retransmit more aggressively - don't wait for RTO/2
                            # In high jitter, we need to fill holes quickly
                            if current_time - send_time > max(0.1, self.rto / 4):
                                self.sock.sendto(packet, client_addr)
                                self.window[seq] = (packet, current_time)
                    
                    # [FIX] Clean up: remove SACKed packets that are far behind base
                    # to free up memory, but keep packets near base_seq
                    for seq in list(self.sacked_packets):
                        if seq < self.base_seq:
                            self.sacked_packets.discard(seq)
                        elif seq >= self.base_seq and seq in self.window:
                            # Keep in window for now, will be removed when base advances
                            pass
                
            except socket.timeout:
                pass
            except Exception as e:
                pass
        
        print(f"File transfer complete. Sent {total_packets} packets.")
        
        # [FIX] Wait longer to ensure client receives final packets
        # especially important with high jitter
        time.sleep(0.5)
    
    def run(self):
        """Main server loop"""
        print(f"Server listening on {self.server_ip}:{self.server_port}")
        print(f"Sender window size: {self.sws} bytes")
        
        # Wait for client request
        try:
            self.sock.settimeout(10.0)  # 10 second timeout for initial request
            data, client_addr = self.sock.recvfrom(1024)
            print(f"Received request from {client_addr}")
            
            # Send the file
            self.send_file(client_addr, 'data.txt')
            
            print("Server finished, exiting")
            
        except socket.timeout:
            print("Server timeout waiting for client")
        except KeyboardInterrupt:
            print("\nServer shutting down")
        finally:
            self.sock.close()

def main():
    if len(sys.argv) != 4:
        print("Usage: python3 p1_server.py <SERVER_IP> <SERVER_PORT> <SWS>")
        sys.exit(1)
    
    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    sws = int(sys.argv[3])
    
    server = ReliableUDPServer(server_ip, server_port, sws)
    server.run()

if __name__ == "__main__":
    main()