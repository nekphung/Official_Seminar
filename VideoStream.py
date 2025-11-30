class VideoStream:
    def __init__(self, filename, mode='normal'):
        """
        mode: 'normal' or 'hd'
        normal: 5 bytes length header
        hd: JPEG-like header (\xff\xd8) and footer (\xff\xd9)
        """
        self.filename = filename
        self.mode = mode
        self.frameNum = 0

        try:
            self.file = open(filename, 'rb')
        except:
            raise IOError(f"Cannot open file {filename}")

    def nextFrame(self):
        """Get next frame depending on mode."""
        if self.mode == 'normal':
            # Read 5 bytes for frame length
            data = self.file.read(5)
            if not data:
                return None  # End of file
            framelength = int(data)
            frame = self.file.read(framelength)
            self.frameNum += 1
            return frame

        elif self.mode == 'hd':
            # Read until we find the JPEG header
            while True:
                byte = self.file.read(1)
                if not byte:
                    return None  # EOF
                if byte == b'\xff':
                    next_byte = self.file.read(1)
                    if next_byte == b'\xd8':
                        break  # Start of frame found

            # Start collecting frame bytes
            frame_data = b'\xff\xd8'
            while True:
                chunk = self.file.read(1024)  # read in chunks
                if not chunk:
                    break
                frame_data += chunk
                # Check if frame ends with \xff\xd9
                if b'\xff\xd9' in frame_data[-1024:]:
                    # split at end marker
                    end_index = frame_data.rfind(b'\xff\xd9') + 2
                    frame_data = frame_data[:end_index]
                    break

            self.frameNum += 1
            return frame_data

        else:
            raise ValueError("Mode must be 'normal' or 'hd'")

    def frameNbr(self):
        """Get frame number."""
        return self.frameNum

