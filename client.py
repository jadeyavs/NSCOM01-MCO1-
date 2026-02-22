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
                    self.seq_num = packet.seq_num # sync our expected sequence number? No, keep our own.
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
            
        expected_seq = self.seq_num + 2 # SYN is +0, SYN-ACK is +1 (from server), first data is +2
        
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
                        ack_pkt = Packet(TYPE_ACK, packet.seq_num, self.session_id)
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

    def upload_file(self, filename):
        if not os.path.exists(filename):
            print(f"[!] File not found: {filename}")
            return
            
        print(f"[*] Starting upload of '{filename}'...")
        if not self.connect("UPLOAD", os.path.basename(filename)):
            return
            
        self.seq_num += 1 # Sequence for our first data packet
        
        with open(filename, 'rb') as f:
            while True:
                data_chunk = f.read(MAX_PAYLOAD_SIZE)
                
                if not data_chunk:
                    # Send FIN
                    self.send_fin()
                    break
                    
                data_pkt = Packet(TYPE_DATA, self.seq_num, self.session_id, data_chunk)
                
                # Stop and wait logical loop
                while True:
                    self.sock.sendto(data_pkt.to_bytes(), self.server_addr)
                    
                    try:
                        data, _ = self.sock.recvfrom(MAX_PAYLOAD_SIZE + HEADER_SIZE)
                        ack_pkt = Packet.from_bytes(data)
                        
                        if ack_pkt.session_id != self.session_id: continue
                        
                        if ack_pkt.msg_type == TYPE_ACK and ack_pkt.seq_num == self.seq_num:
                            self.seq_num += 1
                            break # Move to next chunk
                    except socket.timeout:
                        print(f"[!] Timeout waiting for ACK {self.seq_num}. Retransmitting...")

    def send_fin(self):
        fin_pkt = Packet(TYPE_FIN, self.seq_num, self.session_id)
        print(f"[*] Sending FIN to complete upload...")
        
        while True:
            self.sock.sendto(fin_pkt.to_bytes(), self.server_addr)
            try:
                data, _ = self.sock.recvfrom(MAX_PAYLOAD_SIZE + HEADER_SIZE)
                ack_pkt = Packet.from_bytes(data)
                
                if ack_pkt.session_id == self.session_id and ack_pkt.msg_type == TYPE_ACK and ack_pkt.seq_num == self.seq_num:
                    print("[*] Received ACK for FIN. Upload complete.")
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
