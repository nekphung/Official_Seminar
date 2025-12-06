import sys
from time import time
HEADER_SIZE = 12

class RtpPacket:
    def __init__(self):
        # header và payload là thuộc tính instance (không dùng biến class)
        self.header = bytearray(HEADER_SIZE)
        self.payload = b'' # dữ liệu thô

    def encode(self, version, padding, extension, cc, seqnum, marker, pt, ssrc, payload):
        """Encode the RTP packet with header fields and payload."""
        timestamp = int(time())
        header = bytearray(HEADER_SIZE)

        # Byte 0: V(2), P(1), X(1), CC(4)
        header[0] = (version << 6) | (padding << 5) | (extension << 4) | (cc & 0x0F)
        # ta có: 0x0F = 00001111
        # Byte 1: M(1), PT(7)
        header[1] = ((marker & 0x01) << 7) | (pt & 0x7F)

        # Seqnum (16 bit) - big endian
        header[2] = (seqnum >> 8) & 0xFF
        header[3] = seqnum & 0xFF

        # Timestamp (32 bit), là công cụ để phát lại đúng tốc độ, đồng bộ video từ phía server tới client
        header[4] = (timestamp >> 24) & 0xFF
        header[5] = (timestamp >> 16) & 0xFF
        header[6] = (timestamp >> 8) & 0xFF
        header[7] = timestamp & 0xFF

        # SSRC (32 bit), SSRC giúp liên kết các luồng độc lập
        header[8]  = (ssrc >> 24) & 0xFF
        header[9]  = (ssrc >> 16) & 0xFF
        header[10] = (ssrc >> 8) & 0xFF
        header[11] = ssrc & 0xFF

        self.header = header
        # ensure payload is bytes type
        self.payload = payload if isinstance(payload, (bytes, bytearray)) else bytes(payload) # sử dụng toán tử 3 ngôi để đảm bảo rằng payload phải là bytes hoặc là bytearray

    def decode(self, byteStream):
        """Decode the RTP packet. byteStream: bytes or bytearray."""
        bs = bytearray(byteStream) # gói tin
        self.header = bs[:HEADER_SIZE] # lấy từ đầu tới khi tới byte 12 để lấy cái header
        self.payload = bytes(bs[HEADER_SIZE:]) # lấy từ 12 trở đi để lấy ảnh rồi đổi sang byte

    def version(self):
        """Return RTP version."""
        return (self.header[0] >> 6) & 0x03

    def seqNum(self):
        """Return sequence (frame) number."""
        return (self.header[2] << 8) | self.header[3] # lấy ra seqNum của khung ảnh

    def timestamp(self):
        """Return timestamp."""
        return (self.header[4] << 24) | (self.header[5] << 16) | (self.header[6] << 8) | self.header[7]

    def payloadType(self):
        """Return payload type."""
        return self.header[1] & 0x7F

    def getPayload(self):
        """Return payload (bytes)."""
        return bytes(self.payload)

    def getPacket(self):
        """Return RTP packet as bytes (header + payload)."""
        return bytes(self.header) + bytes(self.payload)

    def marker(self):
        """Return the Marker bit (M bit) as 0 or 1."""
        return (self.header[1] >> 7) & 0x01

