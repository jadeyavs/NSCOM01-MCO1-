import socket
import struct
import time
import os
import random
from protocol import *

# Server settings
HOST = '0.0.0.0'
PORT = 8080
TIMEOUT = 2.0  # seconds
SERVER_DIR = 'server_data'

class UDPServer:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.host, self.port))
        self.sock.settimeout(TIMEOUT)
        print(f"[*] Server listening on {self.host}:{self.port}")
        
        if not os.path.exists(SERVER_DIR):
            os.makedirs(SERVER_DIR)
        print(f"[*] Operating directory: '{SERVER_DIR}/'")
        
        # State tracking per session
        self.sessions = {} # session_id -> { 'state', 'seq_num', 'file_obj', 'expected_seq' }
        
        # simulated packet loss (%)
        self.drop_rate = 0.0

    def start(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(MAX_PAYLOAD_SIZE + HEADER_SIZE)
                
                # simulate packet loss
                if random.random() < self.drop_rate:
                    print(f"[!] Simulating packet drop from {addr}")
                    continue
                    
                self.handle_packet(data, addr)
                
            except socket.timeout:
                # server timeout check (e.g., waiting for FIN-ACK but timed out)
                self.check_timeouts()

    def handle_packet(self, data, addr):
        try:
            packet = Packet.from_bytes(data)
            print(f"[-] Received packet Type: {packet.msg_type}, Seq: {packet.seq_num}, Session: {packet.session_id}")
            
            if packet.msg_type == TYPE_SYN:
                self.handle_syn(packet, addr)
            elif packet.session_id in self.sessions:
                session = self.sessions[packet.session_id]
                
                if packet.msg_type == TYPE_DATA:
                    self.handle_data(packet, addr, session)
                elif packet.msg_type in (TYPE_ACK, TYPE_FIN_ACK):
                    self.handle_ack(packet, addr, session)
                elif packet.msg_type == TYPE_FIN:
                    self.handle_fin(packet, addr, session)
            else:
                print(f"[!] Received packet for unknown session {packet.session_id}")
                
        except ValueError as e:
            print(f"[!] Checksum or parsing error from {addr}: {e}")

    def handle_syn(self, packet, addr):
        # A SYN packet expects the payload to contain the operation (upload/download) and filename.
        # Format: "UPLOAD<sep>filename.ext" or "DOWNLOAD<sep>filename.ext"
        payload_str = packet.payload.decode('utf-8')
        try:
            op, filename = payload_str.split('|', 1)
        except ValueError:
            self.send_error(packet.session_id, addr, packet.seq_num + 1, b"Invalid SYN payload format")
            return

        session_id = packet.session_id
        
        # Secure the filename against directory traversal attacks
        secure_filename = os.path.basename(filename)
        filepath = os.path.join(SERVER_DIR, secure_filename)
        
        # Initialize session state based on operation
        if op == "DOWNLOAD":
            if not os.path.exists(filepath):
                self.send_error(session_id, addr, packet.seq_num + 1, b"File not found")
                return
            
            print(f"[*] Starting DOWNLOAD for {secure_filename}, Session: {session_id}")
            self.sessions[session_id] = {
                'addr': addr,
                'state': 'TRANSFERRING',
                'op': 'DOWNLOAD',
                'file_obj': open(filepath, 'rb'),
                'seq_num': packet.seq_num + 1,  # Server's seq number
                'last_acked_seq': packet.seq_num,
                'unacked_packet': None,
                'last_send_time': 0,
            }
            
            # Send SYN-ACK confirming we have the file
            syn_ack = Packet(TYPE_SYN_ACK, packet.seq_num + 1, session_id, b"OK")
            self.sock.sendto(syn_ack.to_bytes(), addr)
            
            # Start sending first data packet immediately after SYN-ACK
            self.send_next_data(session_id)
            
        elif op == "UPLOAD":
            print(f"[*] Starting UPLOAD for {secure_filename}, Session: {session_id}")
            self.sessions[session_id] = {
                'addr': addr,
                'state': 'TRANSFERRING',
                'op': 'UPLOAD',
                'file_obj': open(filepath, 'wb'),
                'expected_seq': packet.seq_num + 1,
            }
            # Send SYN-ACK
            syn_ack = Packet(TYPE_SYN_ACK, packet.seq_num + 1, session_id, b"OK")
            self.sock.sendto(syn_ack.to_bytes(), addr)

    def send_error(self, session_id, addr, seq_num, err_msg):
        err_pkt = Packet(TYPE_ERROR, seq_num, session_id, err_msg)
        self.sock.sendto(err_pkt.to_bytes(), addr)
        print(f"[!] Sent ERROR to {addr}: {err_msg}")

    def send_next_data(self, session_id):
        session = self.sessions[session_id]
        if session['state'] != 'TRANSFERRING' or session['op'] != 'DOWNLOAD':
            return
            
        if session['unacked_packet'] is not None:
             # Waiting for ACK on this packet still. The check_timeouts logic handles retransmission.
             return

        data_chunk = session['file_obj'].read(MAX_PAYLOAD_SIZE)
        
        if not data_chunk:
            # Reached EOF, initiate FIN
            print(f"[*] EOF reached for session {session_id}, sending FIN")
            fin_pkt = Packet(TYPE_FIN, session['seq_num'] + 1, session_id)
            self.sock.sendto(fin_pkt.to_bytes(), session['addr'])
            session['state'] = 'FIN_WAIT'
            session['unacked_packet'] = fin_pkt
            session['last_send_time'] = time.time()
            session['seq_num'] += 1
            return
            
        # Create and send data packet
        session['seq_num'] += 1
        data_pkt = Packet(TYPE_DATA, session['seq_num'], session_id, data_chunk)
        self.sock.sendto(data_pkt.to_bytes(), session['addr'])
        
        # Store packet to wait for ACK
        session['unacked_packet'] = data_pkt
        session['last_send_time'] = time.time()

    def handle_ack(self, packet, addr, session):
        # We only really care about ACKs when we are DOWNLOADING (sending file to client)
        if session['op'] == 'DOWNLOAD':
            if session['unacked_packet'] and packet.seq_num == session['unacked_packet'].seq_num:
                # The packet we sent was successfully received!
                session['last_acked_seq'] = packet.seq_num
                session['unacked_packet'] = None
                
                if session['state'] == 'TRANSFERRING':
                    self.send_next_data(packet.session_id)
                elif session['state'] == 'FIN_WAIT':
                    # Received ACK for FIN. We are done!
                    print(f"[*] Received FIN-ACK for session {packet.session_id}. Closing.")
                    session['file_obj'].close()
                    del self.sessions[packet.session_id]

    def handle_data(self, packet, addr, session):
        # We handle DATA packets when we are UPLOADING (receiving file from client)
        if session['op'] == 'UPLOAD' and session['state'] == 'TRANSFERRING':
            
            # Check for ordered delivery (Stop-and-Wait)
            if packet.seq_num == session['expected_seq']:
                # Write to file
                session['file_obj'].write(packet.payload)
                session['expected_seq'] += 1
                
                # Send ACK
                ack_pkt = Packet(TYPE_ACK, packet.seq_num, packet.session_id)
                self.sock.sendto(ack_pkt.to_bytes(), addr)
            elif packet.seq_num < session['expected_seq']:
                # Received an older packet, likely meaning our previous ACK was lost.
                # Resend ACK for the duplicate packet so the client can move on.
                print(f"[!] Duplicate data pkt seq {packet.seq_num}, resending ACK")
                ack_pkt = Packet(TYPE_ACK, packet.seq_num, packet.session_id)
                self.sock.sendto(ack_pkt.to_bytes(), addr)
            else:
                 print(f"[!] Out of order packet seq {packet.seq_num} expected {session['expected_seq']}")

    def handle_fin(self, packet, addr, session):
        if session['op'] == 'UPLOAD':
            print(f"[*] Received FIN for upload session {packet.session_id}")
            # Acknowledge FIN
            ack_pkt = Packet(TYPE_FIN_ACK, packet.seq_num, packet.session_id)
            self.sock.sendto(ack_pkt.to_bytes(), addr)
            
            # Close file and finish
            session['file_obj'].close()
            del self.sessions[packet.session_id]


    def check_timeouts(self):
        """Retransmit unacknowledged packets if timeout exceeded, and cleanup stale sessions."""
        current_time = time.time()
        stale_sessions = []
        
        for session_id, session in self.sessions.items():
            last_activity = session.get('last_send_time', current_time)
            
            # Re-transmit logic
            if session.get('unacked_packet'):
                if current_time - last_activity > TIMEOUT:
                    print(f"[!] Timeout! Retransmitting Seq: {session['unacked_packet'].seq_num}")
                    try:
                        self.sock.sendto(session['unacked_packet'].to_bytes(), session['addr'])
                        session['last_send_time'] = current_time
                    except Exception as e:
                        print(f"[!] Error retransmitting: {e}")
            
            # Stale session cleanup (e.g., client crashed)
            if current_time - last_activity > (TIMEOUT * 5):
                print(f"[!] Session {session_id} timed out and will be closed.")
                stale_sessions.append(session_id)
                
        # Remove stale sessions
        for session_id in stale_sessions:
            try:
                self.sessions[session_id]['file_obj'].close()
            except:
                pass
            del self.sessions[session_id]

if __name__ == "__main__":
    server = UDPServer(HOST, PORT)
    server.start()
