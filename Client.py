import sys
import time
from collections import deque
from tkinter import *
import tkinter.messagebox as tkMessageBox
from PIL import Image, ImageTk
import socket, threading, sys, traceback, os
from RtpPacket import RtpPacket

CACHE_FILE_NAME = "cache-"
CACHE_FILE_EXT = ".jpg"

class Client:
    INIT = 0
    READY = 1
    PLAYING = 2

    SETUP = 0
    PLAY = 1
    PAUSE = 2
    TEARDOWN = 3
    DESCRIBE = 4

    # Cấu hình buffer
    MIN_BUFFER_FRAMES = 10
    MAX_BUFFER_FRAMES = 120

    def __init__(self, master, serveraddr, serverport, rtpport, filename):
        self.master = master
        self.master.protocol("WM_DELETE_WINDOW", self.handler)
        self.createWidgets()

        # connection params
        self.serverAddr = serveraddr
        self.serverPort = int(serverport)
        self.rtpPort = int(rtpport)
        self.fileName = filename

        # RTSP state
        self.state = self.INIT
        self.rtspSeq = 0
        self.sessionId = 0
        self.requestSent = -1
        self.teardownAcked = 0

        # frame/state tracking
        self.frameNbr = 0
        self.rtpBuffer = b''
        self.prevSeqNum = 0
        self.currentFrameNum = 0
        self.sentStop = False

        # event to stop RTP listening loop
        self.playEvent = threading.Event()
        self.playEvent.clear()
        self.bufferReadyEvent = threading.Event()

        # Thêm biến để phát hiện server ngừng gửi
        self.endVideo = False
        self.bufferFullPause = False  # buffer đầy, tạm dừng gửi chờ user Play
        self.lastFrameReceivedTime = 0
        self.frameReceiveTimeout = 2.0  # 2 giây không nhận được frame = server ngừng gửi

        # sockets
        self.rtspSocket = None
        self.rtpSocket = None

        # THÊM: Biến cho HD streaming
        self.bandwidth_stats = {
            'start_time': time.time(),
            'total_bytes': 0,
            'last_check': time.time(),
            'total_packets': 0
        }
        self.total_lost_frames = 0
        self.total_frames_received = 0  # Tổng số frame đã nhận
        self.hd_buffer_size = 150  # Buffer lớn hơn cho HD
        self.hd_min_buffer = 15  # Min buffer cho HD

        self.updateButtons()
        self.setup_buffer_system()
        self.cache_lock = threading.Lock()
        self.connectToServer()

    # Các hàm bổ trợ, cập nhật GUI

    def createWidgets(self):
        # --- Video Frame ---
        self.videoFrame = Frame(self.master)
        self.videoFrame.grid(row=0, column=0, sticky=N + S + E + W, padx=5, pady=5)
        self.master.rowconfigure(0, weight=1)
        self.master.columnconfigure(0, weight=1)
        self.label = Label(self.videoFrame, bg="black")
        self.label.pack(fill=BOTH, expand=True)

        # --- Info: Buffer + Time + Describe ---
        self.infoFrame = Frame(self.master)
        self.infoFrame.grid(row=1, column=0, columnspan=4, pady=5)

        self.bufferLabel = Label(self.infoFrame, text="Buffer: 0/120")
        self.bufferLabel.pack(side=LEFT, padx=5)

        self.bandwidthLabel = Label(self.infoFrame, text="BW: 0 kbps")
        self.bandwidthLabel.pack(side=LEFT, padx=5)

        self.timeLabel = Label(self.infoFrame, text="Time: 00:00")
        self.timeLabel.pack(side=LEFT, padx=5)

        self.networkLabel = Label(self.infoFrame, text="Net: Normal Mode", fg="blue")
        self.networkLabel.pack(side=LEFT, padx=10)

        self.describe = Menubutton(self.infoFrame, text="Describe", relief=RAISED)
        self.describe.pack(side=LEFT, padx=5)
        self.describeMenu = Menu(self.describe, tearoff=0)
        self.describe.config(menu=self.describeMenu)
        self.videoMode = StringVar()
        self.videoMode.set("normal")
        self.describeMenu.add_radiobutton(label="Normal", variable=self.videoMode, value="normal",
                                          command=self.sendDescribe)
        self.describeMenu.add_radiobutton(label="HD", variable=self.videoMode, value="hd", command=self.sendDescribe)

        # --- Control buttons ---
        self.controlFrame = Frame(self.master)
        self.controlFrame.grid(row=2, column=0, columnspan=4, pady=5)
        self.setup = Button(self.controlFrame, width=15, text="Setup", command=self.setupMovie)
        self.setup.pack(side=LEFT, padx=5)
        self.start = Button(self.controlFrame, width=15, text="Play", command=self.playMovie)
        self.start.pack(side=LEFT, padx=5)
        self.pause = Button(self.controlFrame, width=15, text="Pause", command=self.pauseMovie)
        self.pause.pack(side=LEFT, padx=5)
        self.teardown = Button(self.controlFrame, width=15, text="Teardown", command=self.exitClient)
        self.teardown.pack(side=LEFT, padx=5)

    def updateTimeLabel(self):
        """Convert total seconds to MM:SS format and update timeLabel."""
        minutes = int(self.currentPlaybackTime // 60)
        seconds = int(self.currentPlaybackTime % 60)
        time_str = f"Time: {minutes:02d}:{seconds:02d}"
        self.timeLabel.config(text=time_str)

    def updateButtons(self):
        if self.state == self.INIT:
            self.setup.config(state="normal")
            self.describe.config(state="normal")
            self.start.config(state="disabled")
            self.pause.config(state="disabled")
            self.teardown.config(state="disabled")

        elif self.state == self.READY:
            self.setup.config(state="disabled")
            self.describe.config(state="disabled")

            buffer_condition = len(self.frameBuffer) >= self.MIN_BUFFER_FRAMES
            end_video = self.endVideo and len(self.frameBuffer) > 0

            if buffer_condition or end_video:
                self.start.config(state="normal")
            else:
                self.start.config(state="disabled")
            self.pause.config(state="disabled")
            self.teardown.config(state="normal")

        elif self.state == self.PLAYING:
            self.setup.config(state="disabled")
            self.describe.config(state="disabled")
            self.start.config(state="disabled")
            self.pause.config(state="normal")
            self.teardown.config(state="normal")

    def updateBufferLabel(self):
        """Cập nhật Buffer Label"""
        current_length = len(self.frameBuffer)
        buffer_ratio = current_length / self.bufferSize

        fps = 1 / self.currentFrameInterval if self.currentFrameInterval > 0 else 0
        text_content = f"Buffer: {current_length:03d}/{self.bufferSize} ({fps:.1f} fps)"

        # Xác định màu đơn giản
        if buffer_ratio < 0.2:
            color = "red"
        elif buffer_ratio < 0.5:
            color = "orange"
        elif buffer_ratio < 0.8:
            color = "blue"
        else:
            color = "green"

        # Cập nhật GUI
        self.master.after(0, lambda: self.bufferLabel.config(
            text=text_content, fg=color))

    # Yêu Cầu 2: HD Video Streaming

    def analyze_frame_loss(self, seq_num):
        """Phân tích mất gói - đơn giản"""
        if self.prevSeqNum != 0 and seq_num > self.prevSeqNum + 1:
            lost_frames = seq_num - self.prevSeqNum - 1
            self.total_lost_frames += lost_frames
        self.prevSeqNum = seq_num

    def calculate_bandwidth(self, packet_size):
        """Tính băng thông đơn giản"""
        current_time = time.time()

        # Cập nhật thống kê
        self.bandwidth_stats['total_bytes'] += packet_size
        self.bandwidth_stats['total_packets'] += 1

        # Tính bandwidth mỗi 2 giây
        if current_time - self.bandwidth_stats['last_check'] >= 2:
            elapsed = current_time - self.bandwidth_stats['last_check']
            if elapsed > 0:
                bytes_per_sec = self.bandwidth_stats['total_bytes'] / elapsed
                kbps = (bytes_per_sec * 8) / 1000

                # Update bandwidth label
                if hasattr(self, 'bandwidthLabel'):
                    self.bandwidthLabel.config(text=f"BW: {kbps:.0f} kbps")

                    if kbps < 1000:
                        self.bandwidthLabel.config(fg="red")
                    elif kbps < 2000:
                        self.bandwidthLabel.config(fg="orange")
                    else:
                        self.bandwidthLabel.config(fg="green")

                # Reset cho lần sau
                self.bandwidth_stats['total_bytes'] = 0
                self.bandwidth_stats['last_check'] = current_time

    def adjust_for_hd(self):
        """Điều chỉnh cài đặt cho video HD"""
        if self.videoMode.get() == "hd":
            self.bufferSize = self.hd_buffer_size
            self.MIN_BUFFER_FRAMES = self.hd_min_buffer

            self.frameReceiveTimeout = 1.5

            self.baseFrameInterval = 0.033

            # Update network label
            if hasattr(self, 'networkLabel'):
                self.networkLabel.config(text="Net: HD Mode")
        else:
            # Normal mode
            self.bufferSize = self.MAX_BUFFER_FRAMES
            self.MIN_BUFFER_FRAMES = 10
            self.frameReceiveTimeout = 2.0
            self.baseFrameInterval = 0.042  # ~24fps

            if hasattr(self, 'networkLabel'):
                self.networkLabel.config(text="Net: Normal Mode")

    def check_network_quality(self):
        """Kiểm tra chất lượng mạng có đủ cho HD không"""
        if self.videoMode.get() == "hd":
            # Tính bandwidth trung bình
            elapsed = time.time() - self.bandwidth_stats['start_time']
            if elapsed > 5:  # Chỉ kiểm tra sau 5 giây
                avg_kbps = (self.bandwidth_stats['total_bytes'] * 8) / (elapsed * 1000)

                # Cần ít nhất 1.5 Mbps cho HD
                required_bandwidth = 1500

                if avg_kbps < required_bandwidth:
                    if hasattr(self, 'networkLabel'):
                        self.networkLabel.config(text=f"Net: Low ({avg_kbps:.0f}kbps)", fg="red")
                    return False
                else:
                    if hasattr(self, 'networkLabel'):
                        self.networkLabel.config(text=f"Net: Good ({avg_kbps:.0f}kbps)", fg="green")
                    return True
        return True

    def print_statistics(self):
        """In thống kê khi teardown"""
        elapsed_time = time.time() - self.bandwidth_stats['start_time']

        if elapsed_time > 0 and self.total_frames_received > 0:

            loss_rate = (self.total_lost_frames / self.total_frames_received) * 100

            avg_kbps = (self.bandwidth_stats['total_bytes'] * 8) / (elapsed_time * 1000)

            print("\n" + "=" * 50)
            print("VIDEO STREAMING STATISTICS")
            print("=" * 50)
            print(f"Total Frames Received: {self.total_frames_received}")
            print(f"Total Lost Frames: {self.total_lost_frames}")
            print(f"Loss Rate: {loss_rate:.2f}%")
            print(f"Average Bandwidth: {avg_kbps:.0f} kbps")
            print(f"Total Duration: {elapsed_time:.1f} seconds")
            print(f"Total Packets: {self.bandwidth_stats['total_packets']}")
            print(f"Buffer Size Used: {self.bufferSize}")
            print("=" * 50 + "\n")

    # Yêu Cầu 3: Client-Side Caching

    def setup_buffer_system(self):
        """Thiết lập hệ thống buffer"""
        # Buffer cho frames
        self.frameBuffer = deque()
        self.bufferSize = self.MAX_BUFFER_FRAMES

        # Control flags
        self.isReceivingFrames = False
        self.isPlaying = False
        self.frameReceiverThread = None
        self.playbackThread = None

        # Frame timing
        self.baseFrameInterval = 0.042  # 24 fps
        self.currentFrameInterval = self.baseFrameInterval
        self.lastDisplayTime = 0
        self.frameDropCount = 0

        # Playback timing
        self.currentPlaybackTime = 0
        self.startTime = 0
        self.pausedTime = 0
        self.lastBufferAdjustTime = time.time()

        # Buffer monitoring
        self.bufferHistory = deque(maxlen=10)

    def waitForBufferThenPlay(self):
        """Đợi đủ buffer rồi mới play, không còn bufferFullPause."""
        timeout = 15
        start_time = time.time()

        while self.state == self.READY:
            buffer_ready = len(self.frameBuffer) >= self.MIN_BUFFER_FRAMES
            server_has_frames = len(self.frameBuffer) > 0

            if buffer_ready or (self.endVideo and server_has_frames):
                self.startFrameReceiver()
                self.master.after(0, self.bufferAndPlay)
                return

            # Timeout
            if time.time() - start_time > timeout:
                return

            time.sleep(0.1)

    def bufferAndPlay(self):
        """Bắt đầu phát video từ buffer"""
        if self.state == self.READY:
            self.sendRtspRequest(self.PLAY)
            while self.state != self.PLAYING:
                time.sleep(0.01)
            self.startPlayback()

    def startFrameReceiver(self):
        """Start receiving frames immediately"""
        if not self.isReceivingFrames and self.rtpSocket:
            self.isReceivingFrames = True
            self.endVideo = False
            self.lastFrameReceivedTime = time.time()

            self.frameReceiverThread = threading.Thread(
                target=self.listenRtp,
                daemon=True
            )
            self.frameReceiverThread.start()

    def adjustPlaybackSpeed(self):
        """Điều chỉnh tốc độ phát dựa trên buffer hiện tại và lịch sử"""
        current_buffer = len(self.frameBuffer)
        self.bufferHistory.append(current_buffer)

        avg_buffer = sum(self.bufferHistory) / len(self.bufferHistory)

        buffer_ratio = avg_buffer / self.bufferSize

        max_interval = 0.08
        self.currentFrameInterval = max_interval - (max_interval - self.baseFrameInterval) * buffer_ratio

        if self.videoMode.get() == "hd":
            self.currentFrameInterval = max(self.currentFrameInterval, 0.035)

    def stopFrameReceiver(self):
        """Stop receiving frames"""
        self.isReceivingFrames = False

    def startPlayback(self):
        """Bắt đầu phát video từ buffer"""
        if self.isPlaying:
            return

        self.isPlaying = True
        self.playEvent.clear()

        self.startTime = time.time() - self.pausedTime

        self.playbackThread = threading.Thread(
            target=self.playFromBuffer,
            daemon=True
        )
        self.playbackThread.start()

    def stopPlayback(self):
        """Dừng phát video"""
        if not self.isPlaying:
            return

        self.isPlaying = False
        self.playEvent.set()
        self.pausedTime = self.currentPlaybackTime

    def playFromBuffer(self):
        """Phát video từ buffer"""
        last_speed_adjustment = time.time()

        while self.isPlaying and not self.playEvent.is_set():
            currentTime = time.time()

            # Điều chỉnh tốc độ
            if currentTime - last_speed_adjustment > 0.5:
                self.adjustPlaybackSpeed()
                last_speed_adjustment = currentTime

            elapsed = currentTime - self.lastDisplayTime

            if elapsed >= self.currentFrameInterval:
                if self.frameBuffer:
                    # Lấy frame từ buffer
                    frameNbr, frame_data = self.frameBuffer.popleft()
                    if self.frameNbr is not None and frameNbr != self.frameNbr + 1:
                        print(f"Lost frame(s) detected: expected {self.frameNbr + 1}, got {frameNbr}")

                    self.updateBufferLabel()

                    # Ghi frame ra file tạm
                    cachename = self.writeFrame(frame_data)

                    # Cập nhật GUI
                    try:
                        self.updateMovie(cachename)
                    except Exception as e:
                        print("Failed to update frame:", e)

                    self.frameNbr = frameNbr
                    print("Current Seq Num: ", self.frameNbr)

                    # Cập nhật thời gian phát
                    self.currentPlaybackTime = currentTime - self.startTime
                    self.updateTimeLabel()

                    self.lastDisplayTime = currentTime

                else:
                    # Buffer rỗng
                    if self.endVideo:
                        self.isPlaying = False
                        self.state = self.READY
                        self.master.after(0, self.updateButtons)
                        break
                    else:
                        time.sleep(0.1)
            else:
                sleep_time = max(0.001, (self.currentFrameInterval - elapsed) / 2)
                time.sleep(sleep_time)

    # Yêu Cầu 1: Implement the RTSP protocol in the client and implement
    # the RTP packetization in the server.

    def setupMovie(self):
        if self.state == self.INIT:
            self.sendRtspRequest(self.SETUP)

    def playMovie(self):
        if self.state == self.READY:
            if len(self.frameBuffer) < self.MIN_BUFFER_FRAMES:
                threading.Thread(target=self.waitForBufferThenPlay, daemon=True).start()
            else:
                self.bufferAndPlay()

    def listenRtp(self):
        """Nhận frames và đổ vào buffer."""
        while self.isReceivingFrames:
            try:
                self.rtpSocket.settimeout(0.02)
                data, addr = self.rtpSocket.recvfrom(65536)

                if not data:
                    continue

                # Kiểm tra END_OF_VIDEO
                if data == b"END_OF_VIDEO":
                    self.endVideo = True
                    self.isReceivingFrames = False
                    self.master.after(0, self.updateButtons)
                    break

                rtpPacket = RtpPacket()
                rtpPacket.decode(data)
                currFrameNbr = rtpPacket.seqNum()
                markerBit = rtpPacket.marker()
                payload = rtpPacket.getPayload()

                self.calculate_bandwidth(len(data))
                self.check_network_quality()

                if currFrameNbr != self.currentFrameNum:
                    self.rtpBuffer = b''
                    self.currentFrameNum = currFrameNbr

                self.rtpBuffer += payload

                if markerBit == 1:
                    self.lastFrameReceivedTime = time.time()

                    self.analyze_frame_loss(currFrameNbr)

                    if (len(self.frameBuffer) >= self.MIN_BUFFER_FRAMES
                            and not self.sentStop
                            and self.state == self.READY):

                        try:
                            self.rtspSocket.sendall(b"STOP_STREAMING")
                        except:
                            print("Failed to send STOP_STREAMING")

                        self.sentStop = True
                        self.master.after(0, self.updateButtons)

                    # Thêm frame vào buffer
                    if len(self.frameBuffer) < self.bufferSize:
                        self.frameBuffer.append((currFrameNbr, self.rtpBuffer))
                        self.updateBufferLabel()

                        self.total_frames_received += 1

                        if self.state == self.READY and len(self.frameBuffer) >= self.MIN_BUFFER_FRAMES:
                            self.master.after(0, self.updateButtons)

                    self.rtpBuffer = b''

            except socket.timeout:
                continue
            except:
                break

        if self.state == self.READY and len(self.frameBuffer) > 0:
            self.master.after(0, self.updateButtons)

    def exitClient(self):
        """Teardown button handler."""
        if self.state != self.INIT:
            self.sendRtspRequest(self.TEARDOWN)
            self.stopFrameReceiver()
            self.stopPlayback()

            os.remove(CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT)
            self.master.destroy()

        self.print_statistics()

    def pauseMovie(self):
        if self.state == self.PLAYING:
            self.sendRtspRequest(self.PAUSE)
            self.stopPlayback()

    def sendDescribe(self):
        """Gửi DESCRIBE request với kiểm tra HD"""
        if self.state == self.INIT:
            if self.videoMode.get() == "hd":
                if not self.check_network_quality():
                    return

            # Áp dụng cài đặt HD/Normal
            self.adjust_for_hd()

            self.sendRtspRequest(self.DESCRIBE)

    def writeFrame(self, data):
        """Write the received frame to a temp image file. Return the image file."""
        cachename = CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT
        file = open(cachename, "wb")
        file.write(data)
        file.close()
        return cachename

    def updateMovie(self, imageFile):
        """Update the image file as video frame in the GUI."""
        photo = ImageTk.PhotoImage(Image.open(imageFile))
        self.label.configure(image=photo, height=288)
        self.label.image = photo

    def connectToServer(self):
        """Connect to the Server. Start a new RTSP/TCP session."""
        self.rtspSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.rtspSocket.connect((self.serverAddr, self.serverPort))
        except:
            tkMessageBox.showwarning('Connection Failed', 'Connection to \'%s\' failed.' % self.serverAddr)

    def sendRtspRequest(self, requestCode):
        """Send RTSP request to the server."""
        # Update RTSP sequence number
        self.rtspSeq += 1

        # SETUP request
        if requestCode == self.SETUP and self.state == self.INIT:
            threading.Thread(target=self.recvRtspReply, daemon=True).start()
            request = f"SETUP {self.fileName} RTSP/1.0\r\nCSeq: {self.rtspSeq}\r\nTransport: RTP/UDP; client_port={self.rtpPort}"
            self.requestSent = self.SETUP

        # PLAY request
        elif requestCode == self.PLAY and self.state == self.READY:
            request = f"PLAY {self.fileName} RTSP/1.0\r\nCSeq: {self.rtspSeq}\r\nSession: {self.sessionId}"
            self.requestSent = self.PLAY

        # PAUSE request
        elif requestCode == self.PAUSE and self.state == self.PLAYING:
            request = f"PAUSE {self.fileName} RTSP/1.0\r\nCSeq: {self.rtspSeq}\r\nSession: {self.sessionId}"
            self.requestSent = self.PAUSE

        # TEARDOWN request
        elif requestCode == self.TEARDOWN and self.state != self.INIT:
            request = f"TEARDOWN {self.fileName} RTSP/1.0\r\nCSeq: {self.rtspSeq}\r\nSession: {self.sessionId}"
            self.requestSent = self.TEARDOWN

        # DESCRIBE request
        elif requestCode == self.DESCRIBE:
            request = (f"DESCRIBE {self.fileName} RTSP/1.0\r\nCSeq: {self.rtspSeq}\r\n"
                       f"Mode: {self.videoMode.get()}")
            self.requestSent = self.DESCRIBE

        # Send the RTSP request
        self.rtspSocket.sendall(request.encode("utf-8"))
        print('\nData sent:\n' + request)

    def recvRtspReply(self):
        """Receive RTSP reply from the server."""
        while True:
            reply = self.rtspSocket.recv(1024)
            if reply:
                self.parseRtspReply(reply.decode("utf-8"))
            if self.requestSent == self.TEARDOWN:
                self.rtspSocket.shutdown(socket.SHUT_RDWR)
                self.rtspSocket.close()
                break

    def parseRtspReply(self, data):
        """Parse the RTSP reply from the server."""
        lines = data.split('\n')
        seqNum = int(lines[1].split(' ')[1])

        if seqNum == self.rtspSeq:
            session = int(lines[2].split(' ')[1])
            if self.sessionId == 0:
                self.sessionId = session

            if self.sessionId == session:
                if int(lines[0].split(' ')[1]) == 200:
                    if self.requestSent == self.SETUP:
                        print("RTSP State: READY")
                        self.state = self.READY
                        self.updateButtons()
                        self.openRtpPort()
                        self.startFrameReceiver()
                    elif self.requestSent == self.PLAY:
                        self.state = self.PLAYING
                        print("RTSP State: PLAYING")
                        self.updateButtons()
                    elif self.requestSent == self.PAUSE:
                        self.state = self.READY
                        print("RTSP State: READY (paused)")
                        self.updateButtons()
                    elif self.requestSent == self.TEARDOWN:
                        self.state = self.INIT
                        print("RTSP State: INIT (teardown)")
                        self.updateButtons()
                        self.teardownAcked = 1

    def openRtpPort(self):
        """Open RTP socket bound to the client rtpPort."""
        self.rtpSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtpSocket.settimeout(0.5)
        try:
            self.rtpSocket.bind(('', self.rtpPort))
            print("RTP Port opened at:", self.rtpPort)
        except:
            tkMessageBox.showwarning('Unable to Bind', 'Unable to bind RTP PORT=%d' % self.rtpPort)

    def handler(self):
        """Handler on explicitly closing the GUI window."""
        self.stopPlayback()
        self.stopFrameReceiver()

        if tkMessageBox.askokcancel("Quit?", "Are you sure you want to quit?"):
            self.exitClient()
        else:  # When the user presses cancel, resume playing
            if self.state == self.READY and len(self.frameBuffer) > 0:
                self.startPlayback()