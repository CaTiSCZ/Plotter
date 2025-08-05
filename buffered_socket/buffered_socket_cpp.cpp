#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include "buffered_socket.hpp"
#include <tuple>

namespace py = pybind11;

PYBIND11_MODULE(buffered_socket_cpp, m) {
    py::object py_socket = py::module_::import("socket");
    py::object py_socket_timeout = py_socket.attr("timeout");
    py::register_exception<SocketTimeout>(m, "SocketTimeout", py_socket_timeout.ptr());
    py::class_<BufferedSocket>(m, "BufferedSocket")
        .def(py::init<int>(), py::arg("max_size") = 4096)
        .def("bind", &BufferedSocket::bind, py::arg("port"), py::arg("use_my_ip")=false, py::arg("device_ip")="192.168.1.100", py::arg("device_port")=9999)
        .def("close", &BufferedSocket::close)
        .def("sendto", [](BufferedSocket& self, py::bytes data, py::tuple addr) {
            if (addr.size() != 2)
                throw std::runtime_error("Address must be a tuple (ip, port)");
            std::string ip = py::str(addr[0]);
            int port = py::int_(addr[1]);
            std::string s = data;
            self.sendto(std::vector<uint8_t>(s.begin(), s.end()), ip, port);
        }, py::arg("data"), py::arg("address"))
        .def("recvfrom", [](BufferedSocket& self, int bufsize) {
            py::gil_scoped_release release;
            auto result = self.recvfrom(bufsize);
            py::bytes data(reinterpret_cast<const char*>(result.first.data()), result.first.size());
            return std::make_tuple(data, std::make_tuple(result.second.first, result.second.second));
        })
        .def("settimeout", &BufferedSocket::settimeout)
        .def("get_received_count", &BufferedSocket::get_received_count);
}

/*
<%
cfg["sources"] = ["buffered_socket.cpp"]
cfg["dependencies"] = ["buffered_socket.hpp", "winsock_manager.hpp"]
setup_pybind11(cfg)
%>
*/
