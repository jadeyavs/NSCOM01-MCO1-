import socket
import sys
import os
import random
import time
from protocol import *

TIMEOUT = 2.0
host = "127.0.0.1"
port = 8080

class UDPClient:
    def __init__(self, host, port):
        self.server_addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(TIMEOUT)
        
        # generate a random session id for this connection
        self.session_id = random.randint(1, 10000)
        self.seq_num = random.randint(1, 100)
        
        # simulated packet drop
        self.drop_rate = 0.0

    def connect(self, op, filename):
        """Initiate the connection with SYN."""
        payload = f"{op}|{filename}".encode('utf-8')
        syn_pkt = Packet(TYPE_SYN, self.seq_num, self.session_id, payload)
        
        print(f"[*] Sending SYN (op={op}, file={filename})...")
        while True:
            self.sock.sendto(syn_pkt.to_bytes(), self.server_addr)
            try:
                data, _ = self.sock.recvfrom(MAX_PAYLOAD_SIZE + HEADER_SIZE)
                packet = Packet.from_bytes(data)
                
                if packet.session_id != self.session_id:
                    continue # Not our session
                    
                if packet.msg_type == TYPE_SYN_ACK:
                    print(f"[*] Received SYN-ACK. Connection established.")
                    # Store the server's SYN-ACK seq separately.
                    # Client and server maintain independent sequence counters.
                    self.server_seq = packet.seq_num
                    return True
                elif packet.msg_type == TYPE_ERROR:
                    print(f"[!] Server Error: {packet.payload.decode('utf-8')}")
                    return False
                    
            except socket.timeout:
                print("[!] SYN Timeout. Retrying...")
            except ValueError as e:
                print(f"[!] Invalid packet received: {e}")

    def download_file(self, filename):
        print(f"[*] Starting download of '{filename}'...")
        if not self.connect("DOWNLOAD", filename):
            return
            
        # Server's first DATA packet has seq = server's SYN-ACK seq + 1.
        # (Client and server have independent sequence counters.)
        expected_seq = self.server_seq + 1
        
        with open("downloaded_" + os.path.basename(filename), 'wb') as f:
            while True:
                try:
                    data, _ = self.sock.recvfrom(MAX_PAYLOAD_SIZE + HEADER_SIZE)
                    
                    if random.random() < self.drop_rate:
                        print("[!] Simulating packet drop (client-side)")
                        continue
                        
                    packet = Packet.from_bytes(data)
                    
                    if packet.session_id != self.session_id:
                        continue
                        
                    if packet.msg_type == TYPE_DATA:
                        if packet.seq_num == expected_seq:
                            # Write data and send ACK
                            f.write(packet.payload)
                            ack_pkt = Packet(TYPE_ACK, packet.seq_num, self.session_id)
                            self.sock.sendto(ack_pkt.to_bytes(), self.server_addr)
                            expected_seq += 1
                        elif packet.seq_num < expected_seq:
                             # Duplicate packet, resend ACK
                             print(f"[!] Duplicate DATA seq {packet.seq_num}, resending ACK")
                             ack_pkt = Packet(TYPE_ACK, packet.seq_num, self.session_id)
                             self.sock.sendto(ack_pkt.to_bytes(), self.server_addr)
                             
                    elif packet.msg_type == TYPE_FIN:
                        print(f"[*] Received FIN. Closing connection.")
                        ack_pkt = Packet(TYPE_FIN_ACK, packet.seq_num, self.session_id)
                        self.sock.sendto(ack_pkt.to_bytes(), self.server_addr)
                        print(f"[*] Download completed successfully.")
                        break
                        
                    elif packet.msg_type == TYPE_ERROR:
                        print(f"[!] Server Error: {packet.payload.decode('utf-8')}")
                        break
                        
                except socket.timeout:
                    # Timeout during download is usually handled by the server retransmitting,
                    # but if it goes on too long, we might assume connection lost.
                    print("[!] Waiting for data from server...")
                except ValueError as e:
                    print(f"[!] Parsing error: {e}")

    def upload_file(self, filename, window_size=4):
        """Upload a file using a Go-Back-N sliding window (window_size packets in flight)."""
        if not os.path.exists(filename):
            print(f"[!] File not found: {filename}")
            return
            
        print(f"[*] Starting upload of '{filename}' (window_size={window_size})...")
        if not self.connect("UPLOAD", os.path.basename(filename)):
            return
            
        # Client's first data packet = SYN seq + 1 (our own counter, not server's).
        # Server's expected_seq for upload is also packet.seq_num + 1 = SYN seq + 1. ✓
        self.seq_num += 1  # First data packet = SYN seq + 1
        
        # Read all chunks up-front (or lazily via a deque for large files)
        with open(filename, 'rb') as f:
            chunks = []
            while True:
                chunk = f.read(MAX_PAYLOAD_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
        
        total_chunks = len(chunks)
        base = 0          # index of oldest unacknowledged packet
        next_idx = 0      # index of next packet to send
        base_seq = self.seq_num  # seq_num corresponding to chunks[0]
        
        # Pre-build all packets
        packets = []
        for i, chunk in enumerate(chunks):
            packets.append(Packet(TYPE_DATA, base_seq + i, self.session_id, chunk))
        
        self.sock.settimeout(TIMEOUT)
        last_send_time = time.time()
        
        while base < total_chunks:
            # Fill the window — send any unsent packets within base..base+window_size
            while next_idx < total_chunks and next_idx < base + window_size:
                print(f"[>] Sending DATA seq {packets[next_idx].seq_num} ({next_idx+1}/{total_chunks})")
                self.sock.sendto(packets[next_idx].to_bytes(), self.server_addr)
                next_idx += 1
            
            if next_idx == base:
                last_send_time = time.time()
            
            try:
                data, _ = self.sock.recvfrom(MAX_PAYLOAD_SIZE + HEADER_SIZE)
                ack_pkt = Packet.from_bytes(data)
                
                if ack_pkt.session_id != self.session_id:
                    continue
                
                if ack_pkt.msg_type == TYPE_ACK:
                    acked_seq = ack_pkt.seq_num
                    acked_idx = acked_seq - base_seq
                    if 0 <= acked_idx < total_chunks and acked_idx >= base:
                        base = acked_idx + 1  # Advance window base (cumulative ACK)
                        last_send_time = time.time()
                        
            except socket.timeout:
                # Timeout: Go-Back-N — retransmit from base
                print(f"[!] Timeout waiting for ACK {packets[base].seq_num}. Retransmitting from seq {packets[base].seq_num}...")
                next_idx = base  # Reset next send pointer to window base
        
        # Update seq_num to after the last chunk
        self.seq_num = base_seq + total_chunks
        self.send_fin()

    def send_fin(self):
        fin_pkt = Packet(TYPE_FIN, self.seq_num, self.session_id)
        print(f"[*] Sending FIN to complete upload...")
        
        while True:
            self.sock.sendto(fin_pkt.to_bytes(), self.server_addr)
            try:
                data, _ = self.sock.recvfrom(MAX_PAYLOAD_SIZE + HEADER_SIZE)
                ack_pkt = Packet.from_bytes(data)
                
                if ack_pkt.session_id == self.session_id and ack_pkt.msg_type == TYPE_FIN_ACK and ack_pkt.seq_num == self.seq_num:
                    print("[*] Received FIN-ACK. Upload complete.")
                    break
            except socket.timeout:
                print("[!] Timeout waiting for FIN ACK. Retransmitting FIN...")


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: python client.py <host> <port> <upload|download> <filename>")
        sys.exit(1)
        
    h = sys.argv[1]
    p = int(sys.argv[2])
    op = sys.argv[3].upper()
    file_name = sys.argv[4]
    
    client = UDPClient(h, p)
    
    if op == "DOWNLOAD":
        client.download_file(file_name)
    elif op == "UPLOAD":
        client.upload_file(file_name)
    else:
        print("Invalid operation. Must be 'upload' or 'download'")
