#pragma once
#include <winsock2.h>
#include <ws2tcpip.h>
#include <thread>
#include <mutex>
#include <queue>
#include <atomic>
#include <condition_variable>
#include <vector>
#include <utility>
#include <string>

class SocketTimeout : public std::runtime_error {
public:
    explicit SocketTimeout(const std::string& msg) : std::runtime_error(msg) {}
};

class BufferedSocket {
public:
    BufferedSocket(int max_size = 4096);
    ~BufferedSocket();

    std::pair<std::string, int> bind(int port, bool use_my_ip = false, const std::string& device_ip = "192.168.1.100", int device_port = 9999);
    void close();

    void sendto(const std::vector<uint8_t>& data, const std::string& ip, int port);
    std::pair<std::vector<uint8_t>, std::pair<std::string, int>> recvfrom(int bufsize);

    void settimeout(double timeout_sec);
    int get_received_count();

private:
    void listen_loop();
    void send_loop();
    void start();

    int max_size_;
    std::atomic<bool> running_;
    SOCKET sock_;
    std::mutex sock_mutex_;

    std::queue<std::pair<std::vector<uint8_t>, sockaddr_in>> receive_buffer_;
    std::queue<std::pair<std::vector<uint8_t>, sockaddr_in>> send_buffer_;
    std::mutex recv_mutex_, send_mutex_;
    std::condition_variable recv_cv_, send_cv_;

    std::thread listener_thread_, sender_thread_;
    double timeout_;
    std::atomic<int> received_count_;
};