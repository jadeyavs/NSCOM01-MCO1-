import struct
import hashlib

# Message Types
TYPE_SYN = 0
TYPE_SYN_ACK = 1
TYPE_DATA = 2
TYPE_ACK = 3
TYPE_FIN = 4
TYPE_FIN_ACK = 5
TYPE_ERROR = 6

# Constants
MAX_PAYLOAD_SIZE = 1024  # Max size of data payload
HEADER_SIZE = 12         # 1(Type) + 4(Seq) + 4(SessionID) + 2(Len) + 1(Checksum)

class Packet:
    def __init__(self, msg_type, seq_num, session_id, payload=b""):
        self.msg_type = msg_type
        self.seq_num = seq_num
        self.session_id = session_id
        self.payload = payload
        self.payload_length = len(payload)
        self.checksum = self._calculate_checksum()

    def _calculate_checksum(self):
        """
        Calculate a simple XOR checksum for the header and payload.
        This provides basic data integrity checking.
        """
        # Pack header without checksum
        header = struct.pack('!B I I H', self.msg_type, self.seq_num, self.session_id, self.payload_length)
        
        # Calculate XOR sum of all bytes in header + payload
        checksum = 0
        for b in header:
            checksum ^= b
        for b in self.payload:
            checksum ^= b
            
        return checksum

    def to_bytes(self):
        """Serialize the packet to a byte string for network transmission."""
        # ! indicates network byte order (big-endian)
        # B = unsigned char (1 byte) Type
        # I = unsigned int (4 bytes) SeqNum
        # I = unsigned int (4 bytes) SessionID
        # H = unsigned short (2 bytes) PayloadLen
        # B = unsigned char (1 byte) Checksum
        header = struct.pack('!B I I H B', 
                             self.msg_type, 
                             self.seq_num, 
                             self.session_id, 
                             self.payload_length, 
                             self.checksum)
        return header + self.payload

    @classmethod
    def from_bytes(cls, data):
        """Deserialize a byte string back into a Packet object."""
        if len(data) < HEADER_SIZE:
            raise ValueError("Packet too short")

        header = data[:HEADER_SIZE]
        payload = data[HEADER_SIZE:]

        msg_type, seq_num, session_id, payload_length, received_checksum = struct.unpack('!B I I H B', header)

        # Truncate payload if it's longer than stated in header (safety)
        payload = payload[:payload_length]

        # Reconstruct packet to verify checksum
        packet = cls(msg_type, seq_num, session_id, payload)
        
        if packet.checksum != received_checksum:
            raise ValueError(f"Checksum mismatch! Expected {packet.checksum}, got {received_checksum}")

        return packet
