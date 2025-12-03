from random import randint
import sys, traceback, threading, socket

from VideoStream import VideoStream
from RtpPacket import RtpPacket

class ServerWorker:
    SETUP = 'SETUP'
    PLAY = 'PLAY'
    PAUSE = 'PAUSE'
    TEARDOWN = 'TEARDOWN'
    DESCRIBE = 'DESCRIBE'

    INIT = 0
    READY = 1
    PLAYING = 2

    OK_200 = 0
    FILE_NOT_FOUND_404 = 1
    CON_ERR_500 = 2

    def __init__(self, clientInfo):
        # clientInfo expected: {'rtspSocket': (connSocket, (addr,port)), ...}
        self.clientInfo = clientInfo
        self.state = self.INIT
        self.mode = 'normal'

    def run(self): # chạy hàm này
        threading.Thread(target=self.recvRtspRequest, daemon=True).start() # bắt đầu xử lý trong luồng

    def recvRtspRequest(self):
        """Receive RTSP request from the client."""
        connSocket = self.clientInfo['rtspSocket'][0]

        while True:
            try:
                data = connSocket.recv(256)

                # Client đóng socket → thoát thread mà không crash
                if not data:
                    print("RTSP connection closed by client.")
                    break

                print("Data received:\n" + data.decode("utf-8"))
                self.processRtspRequest(data.decode("utf-8"))

            except ConnectionResetError:
                print("RTSP connection reset by client.")
                break

            except Exception as e:
                print("RTSP recv error:", e)
                break

    def processRtspRequest(self, data):
        """Process RTSP request sent from the client."""
        # Get the request type
        request = data.split('\n')
        line1 = request[0].split(' ')
        requestType = line1[0]

        # Get the media file name
        filename = line1[1]

        # Get the RTSP sequence number
        seq = request[1].split(' ') # số yêu cầu

        # --- SETUP ---
        if requestType == self.SETUP:
            if self.state == self.INIT:
                print("processing SETUP\n")
                try:
                    self.clientInfo['videoStream'] = VideoStream(filename, mode=self.mode)
                    self.state = self.READY
                except IOError:
                    self.replyRtsp(self.FILE_NOT_FOUND_404, seq[1])
                    return

                self.clientInfo['session'] = randint(100000, 999999)
                self.replyRtsp(self.OK_200, seq[1])

                self.clientInfo['rtpPort'] = int(request[2].split('=')[1].strip())
                self.clientInfo["rtpSocket"] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

                # Event điều khiển gửi frame
                self.clientInfo['event'] = threading.Event()
                self.clientInfo['event'].set()  # ban đầu tạm dừng gửi frame

                # Luồng gửi RTP
                self.clientInfo['worker'] = threading.Thread(target=self.sendRtp, daemon=True)
                self.clientInfo['worker'].start()

        # --- PLAY ---
        elif requestType == self.PLAY:
            if self.state == self.READY:
                print("processing PLAY\n")
                self.state = self.PLAYING
                self.clientInfo['event'].clear()  # bắt đầu gửi frame
                self.replyRtsp(self.OK_200, seq[1])

        # --- PAUSE ---
        elif requestType == self.PAUSE:
            if self.state == self.PLAYING:
                print("processing PAUSE\n")
                self.state = self.READY
                self.replyRtsp(self.OK_200, seq[1])

        # --- TEARDOWN ---
        elif requestType == self.TEARDOWN:
            print("processing TEARDOWN\n")
            self.clientInfo['event'].set()  # tạm dừng và dừng hoàn toàn luồng
            self.replyRtsp(self.OK_200, seq[1])
            self.clientInfo['rtpSocket'].close()


        elif requestType == self.DESCRIBE:
            print("processing DESCRIBE\n")
            self.mode = 'normal'
            for line in request[2:]:
                if line.upper().startswith("MODE:"):
                    self.mode = line.split(":", 1)[1].strip()
                    break
            self.replyRtsp(self.OK_200, seq[1])

    def sendRtp(self):
        """Send RTP packets over UDP (fragmenting large frames)."""
        MAX_RTP_PAYLOAD = 1500 # gửi tối đa bao nhiêu bytes
        event = self.clientInfo.get('event') # điều khiển luồng gửi video
        video = self.clientInfo.get('videoStream') # lấy video mà client yêu cầu
        rtp_socket = self.clientInfo.get('rtpSocket') # lấy ra cái socket mà để server gửi ảnh tới client
        if not (event and video and rtp_socket): # nếu không có thông tin của những cái này thì nó sẽ bị dừng lại
            print("sendRtp: missing event/video/rtp_socket")
            return

        event.clear()

        while True:
            # wait short time; if event set -> stop
            was_set = event.wait(0.05)  # chờ một xíu
            if was_set or event.is_set():
                print("Stopping RTP transmission (TEARDOWN)")
                break

            data = video.nextFrame()  # đọc cái khung tiếp theo

            if not data:
                print("End of video reached. Stopping RTP stream.")
                break  # dừng luồng, không reset

            frameNumber = video.frameNbr() # lấy ra cái số thứ tự của khung
            frame_size = len(data) # chiều dài của khung theo số nguyên, lấy ra chiều dài của khung
            num_chunks = (frame_size + MAX_RTP_PAYLOAD - 1) // MAX_RTP_PAYLOAD # chia khung đó ra thành nhiều khung để truyền gói đó đi

            # get client address (from RTSP socket info)
            try:
                address = self.clientInfo['rtspSocket'][1][0] # lấy cái địa chỉ của client
                port = int(self.clientInfo.get('rtpPort', 0)) # lấy cổng rtp của client
            except:
                print("Connection Error")
            # print('-'*60)
            # traceback.print_exc(file=sys.stdout)
            # print('-'*60)

            for i in range(num_chunks):
                start = i * MAX_RTP_PAYLOAD
                end = min((i + 1) * MAX_RTP_PAYLOAD, frame_size)
                payload_chunk = data[start:end] # phân mảnh dữ liệu trong video
                marker_bit = 1 if (i == num_chunks - 1) else 0 # đánh dấu là gói cuối cùng được truyền

                try:
                    rtp_packet = self.makeRtp(payload_chunk, frameNumber, marker_bit) # nếu là 1 thì là kết thúc chuỗi, không truyền gì là 0
                    # ensure bytes
                    if isinstance(rtp_packet, str):
                        rtp_packet = rtp_packet.encode('latin1') # mỗi ký tự string là 1 byte
                    rtp_socket.sendto(rtp_packet, (address, port)) # gửi đến cái rtp của client
                except Exception:
                    print("Connection Error sending RTP chunk")
                    traceback.print_exc()
                    # break out of chunk loop on send error to avoid busy-looping
                    break

    def makeRtp(self, payload, frameNbr, marker=0):# mặc định marker là 0, nếu là 1 đó là gói cuối cùng của khung
        """RTP-packetize the video data."""
        version = 2 # phiên bản của RTP
        padding = 0 # có đệm thêm gì không hoặc là alignment
        extension = 0 # phần mở rộng
        cc = 0
        pt = 26  # MJPEG # payload type, kiểu dữ liệu gửi là gì
        seqnum = frameNbr # số thứ tự của khung
        ssrc = 0 # SSRC giúp liên kết các luồng độc lập

        rtpPacket = RtpPacket() # tạo ra gói tin Rtp
        rtpPacket.encode(version, padding, extension, cc, seqnum, marker, pt, ssrc, payload) # mã hóa, đóng gói thành một gói tin hoàn chỉnh
        return rtpPacket.getPacket() # lấy ra cái gói đó để gửi đến client

    def replyRtsp(self, code, seq):
        """Send RTSP reply to the client."""
        if code == self.OK_200:
            # print("200 OK")
            session_id = self.clientInfo.get('session', 0)  # nếu chưa có thì dùng 0
            reply = f'RTSP/1.0 200 OK\nCSeq: {seq}\nSession: {session_id}'
            connSocket = self.clientInfo['rtspSocket'][0] # phản hồi tới client trên cổng này
            connSocket.send(reply.encode()) # gửi phản hồi tới client

        # Error messages
        elif code == self.FILE_NOT_FOUND_404: # nếu không tìm thấy
            print("404 NOT FOUND")
        elif code == self.CON_ERR_500: # nếu tìm thấy
            print("500 CONNECTION ERROR")
