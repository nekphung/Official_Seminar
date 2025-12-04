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
    TARGET_BUFFER_FRAMES = 24
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

        # event to stop RTP listening loop
        self.playEvent = threading.Event()
        self.playEvent.clear()
        self.bufferReadyEvent = threading.Event()

        # Thêm biến để phát hiện server ngừng gửi
        self.serverStoppedSending = False
        self.bufferFullPause = False  # buffer đầy, tạm dừng gửi chờ user Play
        self.lastFrameReceivedTime = 0
        self.frameReceiveTimeout = 2.0  # 2 giây không nhận được frame = server ngừng gửi

        # sockets
        self.rtspSocket = None
        self.rtpSocket = None

        self.updateButtons()
        self.setup_caching_system()
        self.cache_lock = threading.Lock()
        self.connectToServer()

    def setup_caching_system(self):
        """Thiết lập hệ thống caching"""
        # Memory cache
        self.frame_cache = {}
        self.cache_hits = 0
        self.cache_misses = 0

        # Buffer
        self.frameBuffer = deque()
        self.bufferSize = self.MAX_BUFFER_FRAMES

        # Control flags
        self.isReceivingFrames = False
        self.isPlaying = False
        self.frameReceiverThread = None
        self.playbackThread = None

        # Performance tracking
        self.performance_stats = {
            'frames_received': 0,
            'frames_from_cache': 0,
            'start_time': time.time(),
            'last_frame_time': 0
        }

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

    def createWidgets(self):
        # --- Video Frame ---
        self.videoFrame = Frame(self.master)
        self.videoFrame.grid(row=0, column=0, sticky=N + S + E + W, padx=5, pady=5)
        self.master.rowconfigure(0, weight=1)
        self.master.columnconfigure(0, weight=1)
        self.label = Label(self.videoFrame, bg="black")
        self.label.pack(fill=BOTH, expand=True)

        # --- Info: Buffer + Cache + Time + Describe ---
        self.infoFrame = Frame(self.master)
        self.infoFrame.grid(row=1, column=0, columnspan=4, pady=5)

        self.bufferLabel = Label(self.infoFrame, text="Buffer: 0/120")
        self.bufferLabel.pack(side=LEFT, padx=5)
        self.cacheLabel = Label(self.infoFrame, text="Cache: 0%")
        self.cacheLabel.pack(side=LEFT, padx=5)
        self.timeLabel = Label(self.infoFrame, text="Time: 00:00")
        self.timeLabel.pack(side=LEFT, padx=5)

        self.statusLabel = Label(self.infoFrame, text="Status: INIT", fg="blue")
        self.statusLabel.pack(side=LEFT, padx=10)

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
            server_stopped_condition = self.serverStoppedSending and len(self.frameBuffer) > 0

            if buffer_condition or server_stopped_condition:
                self.start.config(state="normal")

                if buffer_condition:
                    # Buffer đủ, sẵn sàng phát
                    self.statusLabel.config(text="Status: Ready to Play", fg="green")
                elif server_stopped_condition:
                    # Server ngừng gửi nhưng vẫn còn frame → phát nốt
                    self.statusLabel.config(text="Status: Play Remaining Frames", fg="orange")
            else:
                self.start.config(state="disabled")
                if len(self.frameBuffer) == 0:
                    self.statusLabel.config(text="Status: Buffering...", fg="red")
            self.pause.config(state="disabled")
            self.teardown.config(state="normal")

        elif self.state == self.PLAYING:
            self.setup.config(state="disabled")
            self.describe.config(state="disabled")
            self.start.config(state="disabled")
            self.pause.config(state="normal")
            self.teardown.config(state="normal")

    def checkServerStoppedSending(self):
        """Kiểm tra xem server có ngừng gửi frame không"""
        if self.isReceivingFrames and self.lastFrameReceivedTime > 0:
            time_since_last_frame = time.time() - self.lastFrameReceivedTime
            if time_since_last_frame > self.frameReceiveTimeout:
                # Server đã ngừng gửi frame
                self.serverStoppedSending = True
                self.isReceivingFrames = False
                print(f"Server stopped sending frames. Time since last frame: {time_since_last_frame:.1f}s")
                self.master.after(0, self.updateButtons)
                return True
        return False

    def sendDescribe(self):
        if self.state == self.INIT:
            self.sendRtspRequest(self.DESCRIBE)

    def setupMovie(self):
        if self.state == self.INIT:
            self.sendRtspRequest(self.SETUP)

    def exitClient(self):
        """Clean up when exiting"""
        self.stopFrameReceiver()
        self.stopPlayback()
        if self.state != self.INIT:
            self.sendRtspRequest(self.TEARDOWN)
        self.playEvent.set()
        try:
            self.master.destroy()
        except:
            pass
        self.cleanup_cache()

    def pauseMovie(self):
        if self.state == self.PLAYING:
            self.sendRtspRequest(self.PAUSE)
            self.stopPlayback()
            print(f"Paused at frame {self.frameNbr}, buffer size {len(self.frameBuffer)}")
            self.statusLabel.config(text="Status: Paused", fg="orange")

    def playMovie(self):
        """PLAY - Xử lý cả lần đầu và resume"""
        if self.state == self.READY:
            if self.bufferFullPause and len(self.frameBuffer) <= self.MIN_BUFFER_FRAMES + 5:
                # buffer day -> resume receiving
                self.bufferFullPause = False
                try:
                    msg = "BUFFER_READY".encode('utf-8')
                    self.rtspSocket.sendall(msg)
                except Exception as e:
                    print("Failed to notify server:", e)
                self.startFrameReceiver() # tiep tuc nhan frame
            elif len(self.frameBuffer) < self.MIN_BUFFER_FRAMES:
                self.statusLabel.config(text="Status: Buffering...", fg="orange")
                print(f"Buffer low ({len(self.frameBuffer)} frames), waiting...")
                threading.Thread(target=self.waitForBufferThenPlay, daemon=True).start() # chạy luồng song song
            else:
                self.bufferAndPlay()

    def waitForBufferThenPlay(self):
        """Đợi buffer đủ rồi mới play hoặc resume nếu bufferFullPause."""
        timeout = 15
        start_time = time.time()

        while self.state == self.READY:
            buffer_ready = len(self.frameBuffer) >= self.MIN_BUFFER_FRAMES
            server_has_frames = len(self.frameBuffer) > 0

            if buffer_ready or (self.serverStoppedSending and server_has_frames):
                # Reset bufferFullPause nếu có
                if self.bufferFullPause:
                    self.bufferFullPause = False
                    try:
                        msg = "BUFFER_READY".encode('utf-8')
                        self.rtspSocket.sendall(msg)
                    except Exception as e:
                        print("Failed to notify server:", e)
                    self.startFrameReceiver()

                self.master.after(0, self.bufferAndPlay)
                return

            if time.time() - start_time > timeout:
                self.master.after(0, lambda: self.statusLabel.config(
                    text="Status: Buffer Timeout", fg="red"))
                print("Buffer timeout")
                return

            time.sleep(0.1)

    def bufferAndPlay(self):
        """Bắt đầu phát video từ buffer"""
        if self.state == self.READY:
            print(f"Starting playback with {len(self.frameBuffer)} frames in buffer")

            self.sendRtspRequest(self.PLAY)
            while self.state != self.PLAYING:
                time.sleep(0.01) # chờ một xíu
            self.startPlayback() # bắt đầu phát video

            self.master.after(0, lambda: self.statusLabel.config(
                text="Status: Playing", fg="green"))

    def startFrameReceiver(self):
        """Start receiving frames immediately"""
        if not self.isReceivingFrames and self.rtpSocket:
            self.isReceivingFrames = True
            self.serverStoppedSending = False
            self.lastFrameReceivedTime = time.time()
            self.master.after(0, lambda: self.statusLabel.config(
                text="Status: Receiving frames...", fg="blue"))
            print("Starting to receive frames...")

            self.frameReceiverThread = threading.Thread(
                target=self.receiveAndCacheFrames,
                daemon=True
            )
            self.frameReceiverThread.start()

    def receiveAndCacheFrames(self):
        """Nhận frames, cache lại, và đổ vào buffer."""
        print("Starting to receive frames from server...")

        while self.isReceivingFrames:
            try:
                self.rtpSocket.settimeout(0.02)
                data, addr = self.rtpSocket.recvfrom(65536)

                if not data:
                    continue

                # Kiểm tra END_OF_VIDEO
                if data == b"END_OF_VIDEO":
                    print("Received END_OF_VIDEO from server")
                    self.serverStoppedSending = True
                    self.isReceivingFrames = False
                    self.master.after(0, self.updateButtons)
                    self.master.after(0, lambda: self.statusLabel.config(text="Status: Video Ended", fg="purple"))
                    break

                rtpPacket = RtpPacket()
                rtpPacket.decode(data)
                currFrameNbr = rtpPacket.seqNum()
                markerBit = rtpPacket.marker()
                payload = rtpPacket.getPayload()

                # Ghép frame theo fragmentation
                if currFrameNbr != self.currentFrameNum:
                    self.rtpBuffer = b''
                    self.currentFrameNum = currFrameNbr

                self.rtpBuffer += payload

                # Nếu frame đã hoàn chỉnh
                if markerBit == 1:
                    self.lastFrameReceivedTime = time.time()
                    frame_hash = rtpPacket.getFrameHash()
                    # Cache frame nếu chưa có
                    """ if frame_hash not in self.frame_cache:
                        self.cache_frame(frame_hash, self.rtpBuffer)"""

                    # Thêm frame vào buffer
                    if len(self.frameBuffer) < self.bufferSize:
                        self.frameBuffer.append((currFrameNbr, self.rtpBuffer, frame_hash))
                        self.updateBufferLabel()

                        if self.state == self.READY and len(self.frameBuffer) >= self.MIN_BUFFER_FRAMES:
                            self.master.after(0, self.updateButtons)
                    else:
                        # Buffer full
                        if not self.bufferFullPause: # không đầy
                            self.bufferFullPause = True
                            self.isReceivingFrames = False  # tạm dừng nhận
                            print("Buffer full. Notifying server to pause sending...")
                            self.master.after(0, lambda: self.statusLabel.config(
                                text="Status: Buffer Full", fg="orange"))
                            try:
                                msg = "BUFFER_FULL".encode('utf-8')
                                self.rtspSocket.sendall(msg)
                            except Exception as e:
                                print("Failed to notify server:", e)

                    # Cập nhật thống kê
                    self.performance_stats['frames_received'] += 1
                    self.performance_stats['last_frame_time'] = time.time()

                    # Update cache GUI
                    if currFrameNbr % 10 == 0:
                        self.update_cache_display()

                    self.rtpBuffer = b''

            except socket.timeout:
                if not self.serverStoppedSending:  # chỉ check nếu server chưa dừng
                    self.checkServerStoppedSending()
                continue
            except Exception as e:
                if self.isReceivingFrames:
                    print(f"Error receiving frame: {e}")
                break

        print("Stopped receiving frames")
        # Cập nhật nút Play khi server ngừng gửi nhưng vẫn còn frame trong buffer
        if self.state == self.READY and len(self.frameBuffer) > 0:
            self.serverStoppedSending = True
            self.master.after(0, self.updateButtons)

    # --- Caching methods ---
    def get_cached_frame(self, frame_hash):
        """Lấy frame từ cache nếu tồn tại"""
        if frame_hash in self.frame_cache:
            self.cache_hits += 1
            return self.frame_cache[frame_hash]
        self.cache_misses += 1
        return None

    def cache_frame(self, frame_hash, frame_data):
        """Lưu frame vào cache"""
        if frame_hash not in self.frame_cache:
            self.frame_cache[frame_hash] = frame_data

            if len(self.frame_cache) > 200:
                oldest_key = next(iter(self.frame_cache))
                del self.frame_cache[oldest_key]

    def update_cache_display(self):
        """Cập nhật cache label"""
        total = self.cache_hits + self.cache_misses
        if total > 0:
            hit_rate = (self.cache_hits / total) * 100
            self.cacheLabel.config(text=f"Cache: {hit_rate:.1f}%")
            if hit_rate > 80:
                self.cacheLabel.config(fg="green")
            elif hit_rate > 60:
                self.cacheLabel.config(fg="orange")
            else:
                self.cacheLabel.config(fg="red")

    def adjustPlaybackSpeed(self):
        """Điều chỉnh tốc độ phát dựa trên buffer hiện tại và lịch sử"""
        current_buffer = len(self.frameBuffer)
        self.bufferHistory.append(current_buffer)

        # Tính trung bình buffer
        avg_buffer = sum(self.bufferHistory) / len(self.bufferHistory)

        # Tỉ lệ buffer (0..1)
        buffer_ratio = avg_buffer / self.bufferSize

        # Mốc min/max interval (giây/frame)
        min_interval = 0.033  # ~30fps max speed
        max_interval = 0.08  # ~12.5fps khi buffer thấp

        # Điều chỉnh tuyến tính
        self.currentFrameInterval = max_interval - (max_interval - min_interval) * buffer_ratio

        # Chỉ in debug khi cần
        # print(f"Buffer ratio: {buffer_ratio:.2f}, Frame interval: {self.currentFrameInterval:.3f}s")

    def stopFrameReceiver(self):
        """Stop receiving frames"""
        self.isReceivingFrames = False
        if hasattr(self, 'statusLabel'):
            self.statusLabel.config(text="Status: Stopped")

    # --- HỆ THỐNG PHÁT VIDEO ---
    def startPlayback(self):
        """Bắt đầu phát video từ buffer"""
        if self.isPlaying:
            return

        self.isPlaying = True
        self.playEvent.clear()

        self.startTime = time.time() - self.pausedTime

        print(f"Starting playback with {len(self.frameBuffer)} frames in buffer...")

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
        print("Playback stopped.")

    def playFromBuffer(self):
        """Phát video từ buffer"""
        frames_displayed = 0
        last_speed_adjustment = time.time()

        print("Starting playback...")

        while self.isPlaying and not self.playEvent.is_set():
            currentTime = time.time()
            current_buffer = len(self.frameBuffer)

            # Điều chỉnh tốc độ
            if currentTime - last_speed_adjustment > 0.5:
                self.adjustPlaybackSpeed()
                last_speed_adjustment = currentTime

            elapsed = currentTime - self.lastDisplayTime

            if elapsed >= self.currentFrameInterval:
                if self.frameBuffer:
                    # Lấy frame từ buffer
                    frameNbr, frame_data, frame_hash = self.frameBuffer.popleft()
                    if self.frameNbr is not None and frameNbr != self.frameNbr + 1:
                        print(f"Lost frame(s) detected: expected {self.frameNbr + 1}, got {frameNbr}")
                    self.frameNbr = frameNbr

                    frames_displayed += 1
                    self.updateBufferLabel()

                    # Chỉ in seq num thôi
                    print(f"Current Seq Num: {frameNbr}")

                    # Kiểm tra cache
                    cached_frame = self.get_cached_frame(frame_hash)
                    if cached_frame:
                        frame_data = cached_frame
                        self.performance_stats['frames_from_cache'] += 1

                    self.cache_frame(frame_hash, frame_data)

                    # Ghi frame ra file tạm
                    cachename = self.writeFrame(frame_data)

                    # Cập nhật GUI
                    try:
                        self.updateMovie(cachename)
                    except Exception as e:
                        print("Failed to update frame:", e)

                    self.frameNbr = frameNbr

                    # Cập nhật thời gian phát
                    self.currentPlaybackTime = currentTime - self.startTime
                    self.updateTimeLabel()

                    self.lastDisplayTime = currentTime

                else:
                    # Buffer rỗng
                    if self.serverStoppedSending:
                        print("Video ended - No more frames in buffer")
                        self.isPlaying = False
                        self.state = self.READY
                        self.master.after(0, self.updateButtons)
                        self.master.after(0, lambda: self.statusLabel.config(text="Status: Video Ended", fg="purple"))
                        break
                    else:
                        # doi them
                        time.sleep(0.1)
            else:
                sleep_time = max(0.001, (self.currentFrameInterval - elapsed) / 2)
                time.sleep(sleep_time)

        print(f"Playback stopped. Total frames displayed: {frames_displayed}")

    def updateBufferLabel(self):
        """Cập nhật Buffer Label - CHỈ HIỂN THỊ SỐ THÔI"""
        current_length = len(self.frameBuffer)
        buffer_ratio = current_length / self.bufferSize

        fps = 1 / self.currentFrameInterval if self.currentFrameInterval > 0 else 0
        # CHỈ hiển thị số, không có [LOW], [HIGH], etc.
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

    def cleanup_cache(self):
        """Hiển thị thống kê cache khi thoát"""
        total_frames = self.cache_hits + self.cache_misses
        if total_frames > 0:
            efficiency = (self.cache_hits / total_frames) * 100
            print(f"\nCache statistics:")
            print(f"Cache efficiency: {efficiency:.1f}%")
            print(f"Cache hits: {self.cache_hits}, misses: {self.cache_misses}")
            print(f"Frames in cache: {len(self.frame_cache)}")
            print(f"Total frames received: {self.performance_stats['frames_received']}")

    def listenRtp(self):
        """Keep for compatibility"""
        while not self.playEvent.is_set():
            try:
                self.rtpSocket.settimeout(0.1)
                data, _ = self.rtpSocket.recvfrom(65536)
            except socket.timeout:
                continue
            except Exception:
                break

    def writeFrame(self, data):
        """Write the received frame to a temp image file. Return the image file."""
        cachename = CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT

        with self.cache_lock:
            try:
                with open(cachename, "wb") as file:
                    file.write(data)
            except Exception as e:
                print("WriteFrame error:", e)

        return cachename

    def updateMovie(self, imageFile):
        """Update the image file as video frame in the GUI."""
        with self.cache_lock:
            try:
                photo = ImageTk.PhotoImage(Image.open(imageFile))
                self.label.configure(image=photo, height=288)
                self.label.image = photo
            except Exception as e:
                print("Failed to update frame:", e)

    def connectToServer(self):
        """Connect to the Server. Start a new RTSP/TCP session."""
        self.rtspSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.rtspSocket.connect((self.serverAddr, self.serverPort))
        except:
            tkMessageBox.showwarning('Connection Failed', 'Connection to \'%s\' failed.' % self.serverAddr)

    def sendRtspRequest(self, requestCode):
        """Send RTSP request to the server."""
        self.rtspSeq += 1

        if requestCode == self.SETUP:
            requestLine = f"SETUP {self.fileName} RTSP/1.0"
        elif requestCode == self.PLAY:
            requestLine = f"PLAY {self.fileName} RTSP/1.0"
        elif requestCode == self.PAUSE:
            requestLine = f"PAUSE {self.fileName} RTSP/1.0"
        elif requestCode == self.TEARDOWN:
            requestLine = f"TEARDOWN {self.fileName} RTSP/1.0"
        elif requestCode == self.DESCRIBE:
            requestLine = f"DESCRIBE {self.fileName} RTSP/1.0"
        else:
            return

        request = requestLine + "\r\nCSeq: " + str(self.rtspSeq)
        if requestCode != self.SETUP:
            request += "\r\nSession: " + str(self.sessionId)
        elif requestCode != self.DESCRIBE:
            request += "\r\nTransport: RTP/UDP; client_port=" + str(self.rtpPort)

        if requestCode == self.DESCRIBE:
            request += "\r\nMode: " + self.videoMode.get()

        self.requestSent = requestCode

        # validate transitions
        valid = False
        if requestCode == self.SETUP and self.state == self.INIT:
            valid = True
        elif requestCode == self.PLAY and self.state == self.READY:
            valid = True
        elif requestCode == self.PAUSE and self.state == self.PLAYING:
            valid = True
        elif requestCode == self.TEARDOWN and self.state != self.INIT:
            valid = True
        elif requestCode == self.DESCRIBE:
            valid = True

        if not valid:
            self.rtspSeq -= 1
            print("Invalid RTSP state transition; request ignored.")
            return

        if requestCode == self.SETUP:
            threading.Thread(target=self.recvRtspReply, daemon=True).start()

        try:
            self.rtspSocket.sendall(request.encode("utf-8"))
            print('\nData sent:\n' + request)
        except Exception as e:
            print("Failed to send RTSP request:", e)
            traceback.print_exc()

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
        print("-" * 20 + "\nServer Reply:\n" + data + "\n" + "-" * 20)
        lines = data.splitlines()
        if len(lines) < 1:
            print("Empty RTSP reply.")
            return

        status_parts = lines[0].split(' ', 2)
        if len(status_parts) < 2:
            print("Malformed status line:", lines[0])
            return

        try:
            status_code = int(status_parts[1])
        except:
            print("Could not parse status code:", status_parts)
            return

        seqNum = None
        session = None
        for line in lines[1:]:
            if line.lower().startswith("cseq"):
                try:
                    seqNum = int(line.split(':', 1)[1].strip())
                except:
                    pass
            elif line.lower().startswith("session"):
                try:
                    session = int(line.split(':', 1)[1].strip())
                except:
                    pass

        if seqNum is None:
            print("CSeq not found in reply.")
            return

        if seqNum == self.rtspSeq:
            if self.sessionId == 0 and session is not None:
                self.sessionId = session

            if session is not None and self.sessionId != session:
                print("Session ID mismatch: received", session, "expected", self.sessionId)
                return

            if status_code == 200:
                if self.requestSent == self.SETUP:
                    self.state = self.READY
                    print("RTSP State: READY")
                    self.updateButtons()
                    self.openRtpPort()
                    self.startFrameReceiver() # bắt đầu nhận khung
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
            else:
                print("RTSP Error: status code", status_code)

    def openRtpPort(self):
        """Open RTP socket bound to the client rtpPort."""
        self.rtpSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtpSocket.settimeout(0.5)
        try:
            self.rtpSocket.bind(('', self.rtpPort))
            print("RTP Port opened at:", self.rtpPort)
        except Exception as e:
            tkMessageBox.showwarning('Unable to Bind', 'Unable to bind RTP PORT=%d: %s' % (self.rtpPort, e))

    def handler(self):
        """Xử lý khi đóng GUI"""
        self.stopPlayback()
        self.stopFrameReceiver()

        if tkMessageBox.askokcancel("Quit?", "Are you sure you want to quit?"):
            self.exitClient()
        else:
            if self.state == self.READY and len(self.frameBuffer) > 0:
                self.startPlayback()
                print("Resumed playback from buffer.")