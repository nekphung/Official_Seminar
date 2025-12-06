class VideoStream:
    def __init__(self, filename, mode='normal'):
        """
        mode: 'normal' or 'hd'
        normal: 5 bytes length header
        hd: JPEG-like header (\xff\xd8) and footer (\xff\xd9)
        """
        self.filename = filename
        try:
            self.file = open(filename, 'rb')
        except:
            raise IOError

        self.frameNum = 0
        self.mode = mode

    def nextFrame(self):
        """Get next frame depending on mode."""
        if self.mode == 'normal':
            # Read 5 bytes for frame length
            data = self.file.read(5) # Get the framelength from the first 5 bits
            if data:
                framelength = int(data)

                # Read the current frame
                data = self.file.read(framelength)
                self.frameNum += 1
            return data

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
            data = b'\xff\xd8'
            while True:
                chunk = self.file.read(1024)  # Get chunk
                if not chunk:
                    break
                data += chunk
                # Check if frame ends with \xff\xd9
                if b'\xff\xd9' in data[-1024:]:
                    # split at end marker
                    end_index = data.rfind(b'\xff\xd9') + 2
                    data = data[:end_index]
                    break

            self.frameNum += 1
            return data

        else:
            raise ValueError

    def frameNbr(self):
        """Get frame number."""
        return self.frameNum

