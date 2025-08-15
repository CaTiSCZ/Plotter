import logging, logging.handlers
import sys, os, tempfile, datetime
from queue import Queue
from threading import Lock
from contextlib import AbstractContextManager

DEFAULT_LOG_FORMAT = "%(asctime)s.%(msecs)03d\t%(levelname)-8s\t%(threadName)-24s\t%(name)-10s\t%(message)s"

TCP_HANDLER_DEFAULT_HOST_IP = "localhost"
TCP_HANDLER_DEFAULT_HOST_PORT = 12344

application_logger = None

logger = None

class PlainTextTCPHandler(logging.handlers.SocketHandler):
    def __init__(self, host: str, port: int | None) -> None:
        super().__init__(host, port)
    def makePickle(self, record):
        message = self.formatter.format(record)
        return message.encode()
    
class CallbackHandler(logging.Handler):
    def __init__(self, sink = None, sink_text = None, level = logging.DEBUG):
        super().__init__(level)
        self.sink = sink
        self.sink_text = sink_text

    def emit(self, record):
        try:
            if self.sink is not None:
                self.sink(record)
        except:
            self.handleError(record)
        try:
            if self.sink_text is not None:
                self.sink_text(self.format(record))
        except:
            self.handleError(record)

class QueueListener(logging.handlers.QueueListener):
    def __init__(self, queue, *handlers: logging.Handler, respect_handler_level: bool = False) -> None:
        super().__init__(queue, *handlers, respect_handler_level=respect_handler_level)
        self.handlers = list(handlers)
        self._lock = Lock()

    def add_handler(self, handler: logging.Handler):
        with self._lock:
            self.handlers.append(handler)

    def handle(self, record: logging.LogRecord) -> None:
        with self._lock:
            return super().handle(record)

class Logging(AbstractContextManager):
    def __init__(self, tcp_host=None, tcp_port=TCP_HANDLER_DEFAULT_HOST_PORT):
        super().__init__()
        self.logger = logging.getLogger(application_logger)
        self.logger.setLevel(logging.DEBUG) # nastavení úrovně vypisování hlášek - global
        self.logger_queue = Queue(-1)
        self.logger_queue_handler = logging.handlers.QueueHandler(self.logger_queue)
        self.logger.addHandler(self.logger_queue_handler)
        
        handlers = []

        self.console_handler = logging.StreamHandler()
        self.console_handler.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT))
        self.console_handler.setLevel(logging.CRITICAL)  # nastavení úrovně vypisování hlášek - console
        handlers.append(self.console_handler)

        try:
            path = os.path.abspath(os.path.dirname(__file__))
            tempdir = os.path.abspath(tempfile.gettempdir())
            if os.path.commonpath([path, tempdir]) == tempdir:
                raise NameError("Log file path is within the temp directory")
            logs_dir = os.path.join(path, "logs")
        except NameError as e:
            # Fallback if __file__ is not defined or in temporary directory
            self.logger.debug(e)
            if len(sys.argv) >= 1:
                logs_dir = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "logs")
            else:
                logs_dir = os.path.abspath("logs")
        os.makedirs(logs_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.log_path = os.path.join(logs_dir, f"{ts}.log")
        #self.logger.info(f"Logging to file: {self.log_path}")
        self.file_handler = logging.FileHandler(self.log_path, "a", encoding="utf-8")
        self.file_handler.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT))
        self.file_handler.setLevel(logging.DEBUG) # nastavení úrovně vypisování hlášek - soubor
        handlers.append(self.file_handler)

        if tcp_host is not None:
            self.log_tcp_handler = PlainTextTCPHandler(tcp_host, tcp_port)
            self.log_tcp_handler.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT + '\n'))
            self.log_tcp_handler.setLevel(logging.DEBUG) # nastavení úrovně vypisování hlášek - TCP
            self.log_tcp_handler.createSocket()
            handlers.append(self.log_tcp_handler)
        else:
            self.log_tcp_handler = None

        self.log_printer = QueueListener(self.logger_queue, *handlers, respect_handler_level=True)
        self.log_printer.start()
        
        self.logger.info('Application start')
        global logger
        logger = self.logger
        
    def __exit__(self, exc_type, exc_value, traceback):
        self.logger.info('Application end')
        self.log_printer.stop()
        if self.log_tcp_handler is not None:
            self.log_tcp_handler.close()
