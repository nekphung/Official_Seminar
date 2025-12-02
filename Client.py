import sys
import time
#from time import time
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
    MIN_BUFFER_FRAMES = 20

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
        self.rtspSeq = 0 # số rtsp
        self.sessionId = 0 # id phiên làm việc giữa client và server
        self.requestSent = -1 # số yêu cầu được gửi từ phía client
        self.teardownAcked = 0 # xác nhận tắt video

        # frame/state tracking
        self.frameNbr = 0 # số lượng khung
        self.rtpBuffer = b''       # buffer for reassembling fragmented frame
        self.prevSeqNum = 0 # số phía trước

        # event to stop RTP listening loop
        self.playEvent = threading.Event() # này là tạo ra một biến đồng bộ để điều khiển hoặc ra tín hiệu cho các luồng khác, Event nó như một công tắc,
        self.playEvent.clear()  # tắt công tắc, chỉ chạy khi bật cờ, sẳn sàng để chạy

        # sockets (initialized later)
        self.rtspSocket = None # socket tcp của client
        self.rtpSocket = None # socket udp của client

        self.updateButtons()
        self.setup_caching_system()
        self.cache_lock = threading.Lock()
        self.connectToServer()  # kết nối tới server để có thể lấy những thông số

    def setup_caching_system(self):
        """Thiết lập hệ thống caching"""
        # Memory cache
        self.frame_cache = {}  # Dictionary: {hash: frame_data}
        self.cache_hits = 0  # Đếm cache hits
        self.cache_misses = 0  # Đếm cache misses
        self.currentFrameNum = 0

        # Buffer với caching - TĂNG KÍCH THƯỚC BUFFER
        self.frameBuffer = deque()  # Queue for frames
        self.bufferSize = 120  # Maximum number of frames in buffer

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
        # Frame timing control
        self.frameInterval = 0.042  # ~24 fps
        self.lastDisplayTime = 0
        self.frameDropCount = 0
        self.currentPlaybackTime = 0  # <--- THÊM BIẾN NÀY ĐỂ THEO DÕI THỜI GIAN
        self.startTime = 0  # <--- THÊM BIẾN NÀY
        self.pausedTime = 0  # <--- THÊM BIẾN NÀY: Lưu thời gian đã phát trước khi pause
        self.bufferReadyEvent = threading.Event()  # <-- Event báo hiệu buffer đã đủ MIN_BUFFER_FRAMES

        print("Initialized client-side caching system")

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

        self.bufferLabel = Label(self.infoFrame, text="Buffer: 0/0")
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
        # Sử dụng f-string với zero padding (02d)
        time_str = f"Time: {minutes:02d}:{seconds:02d}"
        self.timeLabel.config(text=time_str)

    def updateButtons(self):
        # INIT: Describe + Setup
        if self.state == self.INIT:
            self.setup.config(state="normal")
            self.describe.config(state="normal")
            self.start.config(state="disabled")
            self.pause.config(state="disabled")
            self.teardown.config(state="disabled")
        # READY: Play + Teardown
        elif self.state == self.READY:
            self.setup.config(state="disabled")
            self.describe.config(state="disabled")
            self.start.config(state="normal")
            self.pause.config(state="disabled")
            self.teardown.config(state="normal")

        # PLAYING: Pause + Teardown
        elif self.state == self.PLAYING:
            self.setup.config(state="disabled")
            self.describe.config(state="disabled")
            self.start.config(state="disabled")
            self.pause.config(state="normal")
            self.teardown.config(state="normal")

    def sendDescribe(self):
        if self.state == self.INIT:
             self.sendRtspRequest(self.DESCRIBE)

    def setupMovie(self):
        if self.state == self.INIT:
            self.sendRtspRequest(self.SETUP) # gửi yêu cầu rtsp tới server

    """def exitClient(self):
        Teardown button handler.

        self.sendRtspRequest(self.TEARDOWN)
        self.master.destroy() # Close the gui window
        os.remove(CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT) # Delete the cache image from video"""

    def exitClient(self):
        """Clean up when exiting"""
        self.stopFrameReceiver() # dừng luồng nhận frame
        self.stopPlayback() # dừng luồng phát video
        if self.state != self.INIT:
            self.sendRtspRequest(self.TEARDOWN)
        self.playEvent.set() # bật công tắt phát video
        try:
            self.master.destroy()
        except:
            pass
        # Dọn dẹp cache khi thoát
        self.cleanup_cache()

    def pauseMovie(self):
        if self.state == self.PLAYING:
            self.sendRtspRequest(self.PAUSE)
            self.stopPlayback()  # chỉ dừng playFromBuffer
            print(f"Paused at frame {self.frameNbr}, buffer size {len(self.frameBuffer)}")

    """def playMovie(self):
        Play button handler.
        if self.state == self.READY:
            # Create a new thread to listen for RTP packets
            threading.Thread(target=self.listenRtp).start()
            self.playEvent = threading.Event()
            self.playEvent.clear()
            self.sendRtspRequest(self.PLAY)"""

    def bufferAndPlay(self):
        """PLAY: gửi lệnh PLAY, chờ buffer >= MIN_BUFFER_FRAMES, rồi phát"""
        # Reset event
        self.bufferReadyEvent.clear()

        # Gửi lệnh PLAY tới server
        self.sendRtspRequest(self.PLAY)

        # Bắt đầu nhận frame nếu chưa nhận
        if not self.isReceivingFrames:
            self.startFrameReceiver()

        # Cập nhật GUI
        self.master.after(0, lambda: self.statusLabel.config(
            text=f"Status: Buffering... (0/{self.MIN_BUFFER_FRAMES})", fg="orange"))

        # Chờ buffer sẵn sàng
        is_ready = self.bufferReadyEvent.wait(timeout=10)

        if is_ready:
            self.startPlayback()
            self.master.after(0, lambda: self.statusLabel.config(
                text="Status: Playing", fg="green"))
        else:
            self.master.after(0, lambda: self.statusLabel.config(
                text="Status: Buffer Error / Timeout", fg="red"))
            print("Buffer not filled in time.")

    def playMovie(self):
        """PLAY - Khởi động quá trình Buffering và phát"""
        if self.state == self.READY:
            print("Preparing to Play...")
            threading.Thread(target=self.bufferAndPlay, daemon=True).start() # bắt đầu luồng nhận và phát video

    # --- Caching methods ---
    def get_cached_frame(self, frame_hash):
            """Lấy frame từ cache nếu tồn tại"""
            if frame_hash in self.frame_cache:
                self.cache_hits += 1
                return self.frame_cache[frame_hash] # trả về frame đã được lưu trong cache
            self.cache_misses += 1 # nếu như không lấy trong cache thì sẽ bị mất
            return None

    def cache_frame(self, frame_hash, frame_data):
        """Lưu frame vào cache"""
        if frame_hash not in self.frame_cache:
            self.frame_cache[frame_hash] = frame_data

            # Giới hạn kích thước cache
            if len(self.frame_cache) > 200:  # Tăng kích thước cache
                oldest_key = next(iter(self.frame_cache))
                del self.frame_cache[oldest_key]

    def update_cache_display(self):
        """Cập nhật cache label"""
        total = self.cache_hits + self.cache_misses
        if total > 0:
            hit_rate = (self.cache_hits / total) * 100
            self.cacheLabel.config(text=f"Cache: {hit_rate:.1f}%")
            # Đổi màu
            if hit_rate > 80:
                self.cacheLabel.config(fg="green")
            elif hit_rate > 60:
                self.cacheLabel.config(fg="orange")
            else:
                self.cacheLabel.config(fg="red")

    # --- REAL-TIME FRAME RECEIVER ---
    def startFrameReceiver(self):
        """Start receiving frames immediately"""
        if not self.isReceivingFrames and self.rtpSocket:
            self.isReceivingFrames = True
            self.statusLabel.config(text="Status: Receiving frames...")
            print("Starting to receive frames immediately...")

            self.frameReceiverThread = threading.Thread(target=self.receiveAndCacheFrames, daemon=True) # mở luồng để nhận và cache frame
            self.frameReceiverThread.start()
            print("Frame receiver thread started!")

    def receiveAndCacheFrames(self):
        """Nhận frames liên tục, tái lắp ráp, CACHE và điền BUFFER. Cập nhật trạng thái Buffer trên GUI."""
        # Chỉ in log bắt đầu (cần thiết)
        print("Starting to receive frames from server...")

        # Khởi tạo total_frames_received và last_log_time nếu cần thiết cho thống kê khác
        # Nhưng sẽ không sử dụng chúng để in ra console thường xuyên nữa.

        while self.isReceivingFrames:
            try:
                # Giảm timeout để nhận frames nhanh hơn
                self.rtpSocket.settimeout(0.1)
                data, addr = self.rtpSocket.recvfrom(65536) # nhận dữ liệu từ luồng rtp

                if not data:
                    continue

                rtpPacket = RtpPacket()
                rtpPacket.decode(data)
                currFrameNbr = rtpPacket.seqNum()
                markerBit = rtpPacket.marker()
                payload = rtpPacket.getPayload()

                # Xử lý frame fragmentation
                if currFrameNbr != self.currentFrameNum:
                    self.rtpBuffer = b''  # Reset buffer for new frame
                    self.currentFrameNum = currFrameNbr

                self.rtpBuffer += payload

                # Khi frame hoàn chỉnh (marker bit = 1)
                if markerBit == 1:
                    # Tạo hash cho frame để caching
                    frame_hash = rtpPacket.getFrameHash()

                    # Cache frame mới
                    if frame_hash not in self.frame_cache:
                        self.cache_frame(frame_hash, self.rtpBuffer)

                    # Thêm frame vào buffer
                    # Chỉ giới hạn khi đang PLAYING để tránh tràn bộ nhớ
                    if len(self.frameBuffer) < self.bufferSize:
                        self.frameBuffer.append((currFrameNbr, self.rtpBuffer, frame_hash))

                        # >>> LOGIC BÁO HIỆU SẴN SÀNG (Đồng bộ) <<<
                        # Nếu event chưa được đặt VÀ buffer đã đạt ngưỡng MIN_BUFFER_FRAMES (10)
                        if not self.bufferReadyEvent.is_set() and len(self.frameBuffer) >= self.MIN_BUFFER_FRAMES:
                            self.bufferReadyEvent.set() # bật công tắc

                    # Cập nhật thống kê hiệu suất
                    self.performance_stats['frames_received'] += 1
                    self.performance_stats['last_frame_time'] = time.time()

                    # Cập nhật cache display
                    if currFrameNbr % 10 == 0:
                        self.update_cache_display()

                    with open(f"cache-{frame_hash}.jpg", "wb") as f:
                        f.write(self.rtpBuffer)

                    self.rtpBuffer = b''

            except socket.timeout:
                continue
            except Exception as e:
                if self.isReceivingFrames:
                    print(f"Error receiving frame: {e}")
                    traceback.print_exc()
                break

        print("Stopped receiving frames")

    def stopFrameReceiver(self):
        """Stop receiving frames"""
        self.isReceivingFrames = False # đang nhận frame là false
        if hasattr(self, 'statusLabel'):
            self.statusLabel.config(text="Status: Stopped")

    # --- HỆ THỐNG PHÁT VIDEO ---
    def startPlayback(self):
        """Bắt đầu phát video từ buffer"""
        if self.isPlaying:
            return  # Đang phát rồi
        self.isPlaying = True
        self.playEvent.clear() # tắt công tắc

        # CÁCH CHÍNH XÁC HƠN: Đặt startTime để tính offset từ lần cuối Play
        self.startTime = time.time()  # <--- Đặt lại thời gian bắt đầu cho lần Play này

        print(f"Starting playback with {len(self.frameBuffer)} frames in buffer...")

        # Start playback thread
        self.playbackThread = threading.Thread(target=self.playFromBuffer, daemon=True) # mở luồng chạy từ buffer
        self.playbackThread.start()

    def stopPlayback(self):
        """Dừng phát video"""
        if not self.isPlaying:
            return
        self.isPlaying = False
        self.playEvent.set() # bật công tắt dừng phát,
        self.pausedTime = self.currentPlaybackTime
        print("Playback stopped.")

    """def playFromBuffer(self):
        while self.isPlaying and not self.playEvent.is_set():
            currentTime = time()
            elapsed = currentTime - self.lastDisplayTime
            if elapsed >= self.frameInterval:
                if self.frameBuffer:
                    frameNbr, frame_data, frame_hash = self.frameBuffer.popleft()
                    self.updateBufferLabel()

                    cached_frame = self.get_cached_frame(frame_hash)
                    if cached_frame:
                        frame_data = cached_frame
                        self.performance_stats['frames_from_cache'] += 1

                    cachename = self.writeFrame(frame_data)
                    self.updateMovie(cachename)
                    self.frameNbr = frameNbr
                    print("Current Seq Num: ", self.frameNbr)
                    # >>> CẬP NHẬT THỜI GIAN DỰA TRÊN THỜI GIAN THỰC <<<

                    time_since_play_start = currentTime - self.startTime
                    self.currentPlaybackTime = self.pausedTime + time_since_play_start
                    self.updateTimeLabel()

                    self.lastDisplayTime = currentTime"""

    def playFromBuffer(self):
        """Phát video từ buffer, giữ frame rate ~24fps"""
        while self.isPlaying and not self.playEvent.is_set():
            currentTime = time.time()
            elapsed = currentTime - self.lastDisplayTime
            if elapsed >= self.frameInterval:
                if self.frameBuffer:
                    # Lấy frame tiếp theo từ buffer
                    frameNbr, frame_data, frame_hash = self.frameBuffer.popleft()
                    self.updateBufferLabel()

                    # Cache nếu có
                    cached_frame = self.get_cached_frame(frame_hash)
                    if cached_frame:
                        frame_data = cached_frame
                        self.performance_stats['frames_from_cache'] += 1

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
                    time_since_play_start = currentTime - self.startTime
                    self.currentPlaybackTime = self.pausedTime + time_since_play_start
                    self.updateTimeLabel()

                    self.lastDisplayTime = currentTime
                else:
                    time.sleep(0.01)
            else:
                time.sleep(0.005)

    def updateBufferLabel(self):
        """Cập nhật Buffer Label. PHẢI ĐƯỢC GỌI AN TOÀN TỪ LUỒNG PHỤ."""
        # Logic tính toán màu và text
        current_length = len(self.frameBuffer)
        buffer_ratio = current_length / self.bufferSize
        text_content = f"Buffer: {current_length}/{self.bufferSize}"

        if buffer_ratio < 0.1:
            color = "red"
        elif buffer_ratio < 0.3:
            color = "orange"
        elif buffer_ratio < 0.7:
            color = "blue"
        else:
            color = "green"

        # SỬ DỤNG self.master.after() ĐỂ CHẠY TRONG LUỒNG CHÍNH
        self.master.after(0, lambda: self.bufferLabel.config(
            text=text_content, fg=color))

    def cleanup_cache(self):
        """Hiển thị thống kê cache khi thoát"""
        total_frames = self.cache_hits + self.cache_misses
        if total_frames > 0:
            efficiency = (self.cache_hits / total_frames) * 100
            print(f"Cache statistics: {efficiency:.1f}% efficiency")
            print(f"Cache hits: {self.cache_hits}, misses: {self.cache_misses}")
            print(f"Frames in cache: {len(self.frame_cache)}")
            print(f"Total frames received: {self.performance_stats['frames_received']}")
            print(f"Frames dropped: {self.frameDropCount}")

    """def listenRtp(self):
        Listen for RTP packets and reassemble fragmented frames using marker bit.
        while True:
            try:
                self.rtpSocket.settimeout(0.1)
                data, _ = self.rtpSocket.recvfrom(65536)  # larger buffer
                if not data:
                    continue

                rtpPacket = RtpPacket() # tạo một gói này
                rtpPacket.decode(data) # giải mã cái dữ liệu
                currFrameNbr = rtpPacket.seqNum() # lấy ra cái số khung hiện tại
                markerBit = rtpPacket.marker() # bit đánh dấu của khung đó
                # debug
                # print("Current Seq Num:", currFrameNbr, "Marker:", markerBit)

                payload = rtpPacket.getPayload() # lấy dữ liệu thô từ khung

                # if new sequence number (in-order)
                if currFrameNbr > self.prevSeqNum: # Nếu như số khung hiện tại mà lớn hơn số khung trước đó thì thực hiện
                    # detect lost packets
                    if currFrameNbr > self.prevSeqNum + 1 and self.prevSeqNum != 0:
                        print("Packet loss detected: expected", self.prevSeqNum + 1, "got", currFrameNbr)
                        # reset buffer if jumping to new frame
                        self.rtpBuffer = b'' # nếu bị mất thì nhảy tới khung mới luôn

                    # new frame starts — reset buffer then append
                    if currFrameNbr != self.prevSeqNum:
                        self.rtpBuffer = b'' # bắt đầu một khung mới

                    self.prevSeqNum = currFrameNbr # gán lại cho cái số khung trước bằng số khung hiện tại

                # append payload (works for both single-chunk and fragmented frames)
                self.rtpBuffer += payload # thực hiện cộng bytes lại với nhau

                # if marker bit set -> last chunk of frame => assemble and display
                if markerBit == 1: # khung moi
                    self.frameNbr = currFrameNbr
                    print("Current Seq Num: ", currFrameNbr)
                    cachename = self.writeFrame(self.rtpBuffer)
                    self.updateMovie(cachename)
                    # reset buffer for next frame
                    self.rtpBuffer = b'' # nếu nó đã nhận khung thành công thì sẽ reset để nhận ảnh tiếp theo

            except socket.timeout:
                # normal: loop back and check events
                pass
            except OSError as e:
                # socket closed or other OS error -> break
                print("RTP listen OSError:", e)
                break
            except Exception as e:
                print("RTP listen exception:", e)
                traceback.print_exc()
                break

            # stop conditions
            if self.playEvent.is_set():
                break
            if self.teardownAcked == 1:
                # close socket and exit
                try:
                    self.rtpSocket.close()
                except:
                    pass
                break"""

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

        # tránh đọc khi đang ghi
        with self.cache_lock:
            try:
                with open(cachename, "wb") as file:
                    file.write(data)
            except Exception as e:
                print("WriteFrame error:", e)

        return cachename

    def updateMovie(self, imageFile):
        """Update the image file as video frame in the GUI."""

        # tránh đọc khi writeFrame đang ghi
        with self.cache_lock:
            try:
                # kiểm tra file có hợp lệ không
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
        # 1. increment sequence
        self.rtspSeq += 1

        # 2. request line
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

        # build request (CRLF terminated)
        request = requestLine + "\r\nCSeq: " + str(self.rtspSeq)
        if requestCode != self.SETUP:
            request += "\r\nSession: " + str(self.sessionId)
        elif requestCode != self.DESCRIBE:
            request += "\r\nTransport: RTP/UDP; client_port=" + str(self.rtpPort)

        if requestCode == self.DESCRIBE:
            request += "\r\nMode: " + self.videoMode.get()

        self.requestSent = requestCode

        # validate transitions (simple)
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
            # invalid transition: rollback seq and ignore
            self.rtspSeq -= 1
            print("Invalid RTSP state transition; request ignored.")
            return

        # start listener for RTSP replies on SETUP (daemon)
        if requestCode == self.SETUP:
            threading.Thread(target=self.recvRtspReply, daemon=True).start() # mở luồng nhận phản hồi từ server

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
            # Close the RTSP socket upon requesting Teardown
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

        # status line
        status_parts = lines[0].split(' ', 2)
        if len(status_parts) < 2:
            print("Malformed status line:", lines[0])
            return

        try:
            status_code = int(status_parts[1]) # lấy ra code của phản hồi: 200, 404, 400, ....
        except:
            print("Could not parse status code:", status_parts)
            return

        seqNum = None # số lần request
        session = None # phiên làm việc
        for line in lines[1:]:
            if line.lower().startswith("cseq"):
                try:
                    seqNum = int(line.split(':', 1)[1].strip()) # lấy ra số request
                except:
                    pass
            elif line.lower().startswith("session"):
                try:
                    session = int(line.split(':', 1)[1].strip()) # lấy phiên từ server trả về
                except:
                    pass

        if seqNum is None:
            print("CSeq not found in reply.")
            return

        if seqNum == self.rtspSeq: # nếu nó phản hồi thì gán lại session đối với lần đầu phản hồi
            if self.sessionId == 0 and session is not None:
                self.sessionId = session

            if session is not None and self.sessionId != session:
                print("Session ID mismatch: received", session, "expected", self.sessionId)
                return

            if status_code == 200:
                if self.requestSent == self.SETUP:
                    self.state = self.READY
                    self.updateButtons()
                    print("RTSP State: READY")
                    self.openRtpPort() # mở cổng nhận video từ server thông qua RTP/UDP
                    self.startFrameReceiver()
                elif self.requestSent == self.PLAY:
                    self.state = self.PLAYING
                    self.updateButtons()
                    print("RTSP State: PLAYING")
                elif self.requestSent == self.PAUSE:
                    self.state = self.READY
                    self.updateButtons()
                    print("RTSP State: READY (paused)")
                    # signal RTP thread to stop sending/receiving
                    # self.playEvent.set() # dừng luồng truyền video
                elif self.requestSent == self.TEARDOWN:
                    self.state = self.INIT
                    self.updateButtons()
                    print("RTSP State: INIT (teardown)")
                    self.teardownAcked = 1 # gán xác nhận đóng
            else:
                print("RTSP Error: status code", status_code)

    def openRtpPort(self):
        """Open RTP socket bound to the client rtpPort."""
        self.rtpSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtpSocket.settimeout(0.5) # thiết lập thời gian chờ
        try:
            self.rtpSocket.bind(('', self.rtpPort))
            print("RTP Port opened at:", self.rtpPort)
        except Exception as e:
            tkMessageBox.showwarning('Unable to Bind', 'Unable to bind RTP PORT=%d: %s' % (self.rtpPort, e))

    """def handler(self):
        Handler on explicitly closing the GUI window.
        self.pauseMovie()
        if tkMessageBox.askokcancel("Quit?", "Are you sure you want to quit?"):
            self.exitClient()
        else: # When the user presses cancel, resume playing.
            self.playMovie() """

    """def handler(self):
        Handler on explicitly closing the GUI window.
        self.stopPlayback()  # Dừng phát (playFromBuffer)
        self.stopFrameReceiver()  # Dừng nhận (receiveAndCacheFrames)

        if tkMessageBox.askokcancel("Quit?", "Are you sure you want to quit?"):
            self.exitClient()
        else:  # When the user presses cancel, resume playing.
            if self.state == self.READY:
                self.startPlayback()  # Chỉ cần startPlayback, không cần gửi PLAY RTSP lần nữa
                print("Resumed playback from buffer.")
            else:
                # Nếu đang PLAYING (chờ pause ACK) hoặc INIT, thì không làm gì.
                pass"""

    def handler(self):
        """Xử lý khi đóng GUI"""
        self.stopPlayback()
        self.stopFrameReceiver()

        if tkMessageBox.askokcancel("Quit?", "Are you sure you want to quit?"):
            self.exitClient()
        else:
            if self.state == self.READY and len(self.frameBuffer) > 0:
                # Resume playback từ buffer hiện tại
                self.startPlayback()
                print("Resumed playback from buffer.")

