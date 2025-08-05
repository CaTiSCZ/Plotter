# import logging
# import sys
# root_logger = logging.getLogger()
# root_logger.setLevel(logging.DEBUG)
# handler = logging.StreamHandler(sys.stdout)
# handler.setLevel(logging.DEBUG)
# formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
# handler.setFormatter(formatter)
# root_logger.addHandler(handler)

import cppimport

buffered_socket = cppimport.imp("buffered_socket.buffered_socket_cpp")
#import buffered_socket_py as buffered_socket

from buffered_socket_py import test_main

if __name__ == '__main__':
    test_main(buffered_socket.BufferedSocket)
