#include "buffered_socket.hpp"
#include "winsock_manager.hpp"
#include <stdexcept>
#include <chrono>
#include <iostream>
#include <algorithm>

#undef min // defined in minwindef.h; conflicts with std::min from algorithm

#pragma comment(lib, "Ws2_32.lib")

BufferedSocket::BufferedSocket(int max_size)
    : max_size_(max_size), running_(false), sock_(INVALID_SOCKET),
      timeout_(1.0), received_count_(0)
{
    WinsockManager::ensure_initialized();
}

BufferedSocket::~BufferedSocket() {
    close();
}

std::pair<std::string, int> BufferedSocket::bind(int port, bool use_my_ip, const std::string& device_ip, int device_port) {
    close();
    std::lock_guard<std::mutex> lock(sock_mutex_);

    std::string local_ip = "0.0.0.0";
    if (use_my_ip) {
        SOCKET tmp_sock = socket(AF_INET, SOCK_DGRAM, 0);
        if (tmp_sock == INVALID_SOCKET)
            throw std::runtime_error("Cannot create temp socket");
        sockaddr_in remote_addr{};
        remote_addr.sin_family = AF_INET;
        remote_addr.sin_port = htons(device_port);
        inet_pton(AF_INET, device_ip.c_str(), &remote_addr.sin_addr);
        connect(tmp_sock, (sockaddr*)&remote_addr, sizeof(remote_addr));

        sockaddr_in local_addr{};
        int len = sizeof(local_addr);
        getsockname(tmp_sock, (sockaddr*)&local_addr, &len);
        char ipbuf[INET_ADDRSTRLEN];
        inet_ntop(AF_INET, &local_addr.sin_addr, ipbuf, sizeof(ipbuf));
        local_ip = ipbuf;
        closesocket(tmp_sock);
    }

    sock_ = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock_ == INVALID_SOCKET)
        throw std::runtime_error("Cannot create socket");

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, local_ip.c_str(), &addr.sin_addr);

    if (::bind(sock_, (sockaddr*)&addr, sizeof(addr)) < 0) {
        closesocket(sock_);
        sock_ = INVALID_SOCKET;
        throw std::runtime_error("Bind failed");
    }

    DWORD tv = (DWORD)(timeout_ * 1000);
    setsockopt(sock_, SOL_SOCKET, SO_RCVTIMEO, (const char*)&tv, sizeof(tv));

    start();

    return {local_ip, port};
}

void BufferedSocket::close() {
    running_ = false;
    if (sock_ != INVALID_SOCKET) {
        closesocket(sock_);
        sock_ = INVALID_SOCKET;
    }
    if (listener_thread_.joinable()) listener_thread_.join();
    if (sender_thread_.joinable()) sender_thread_.join();
    // Empty buffers:
    {
        std::lock_guard<std::mutex> l(recv_mutex_);
        std::queue<std::pair<std::vector<uint8_t>, sockaddr_in>>().swap(receive_buffer_);
    }
    {
        std::lock_guard<std::mutex> l(send_mutex_);
        std::queue<std::pair<std::vector<uint8_t>, sockaddr_in>>().swap(send_buffer_);
    }
}

void BufferedSocket::start() {
    if (sock_ == INVALID_SOCKET) throw std::runtime_error("Call bind() first.");
    running_ = true;
    listener_thread_ = std::thread(&BufferedSocket::listen_loop, this);
    sender_thread_ = std::thread(&BufferedSocket::send_loop, this);
}

void BufferedSocket::listen_loop() {
    while (running_) {
        sockaddr_in src_addr{};
        int addrlen = sizeof(src_addr);
        std::vector<uint8_t> buffer(max_size_);
        int ret = ::recvfrom(sock_, (char*)buffer.data(), max_size_, 0, (sockaddr*)&src_addr, &addrlen);
        if (ret > 0) {
            buffer.resize(ret);
            {
                std::lock_guard<std::mutex> l(recv_mutex_);
                receive_buffer_.emplace(std::move(buffer), src_addr);
                received_count_++;
            }
            recv_cv_.notify_one();
        }
        // else timeout or error, just continue
    }
}

void BufferedSocket::send_loop() {
    while (running_) {
        std::pair<std::vector<uint8_t>, sockaddr_in> item;
        {
            std::unique_lock<std::mutex> l(send_mutex_);
            send_cv_.wait_for(l, std::chrono::milliseconds(100), [&]{ return !send_buffer_.empty() || !running_; });
            if (!running_) break;
            if (send_buffer_.empty()) continue;
            item = std::move(send_buffer_.front());
            send_buffer_.pop();
        }
        ::sendto(sock_, (const char*)item.first.data(), (int)item.first.size(),
               0, (sockaddr*)&item.second, sizeof(item.second));
    }
}

void BufferedSocket::sendto(const std::vector<uint8_t>& data, const std::string& ip, int port) {
    sockaddr_in dst_addr{};
    dst_addr.sin_family = AF_INET;
    dst_addr.sin_port = htons(port);
    inet_pton(AF_INET, ip.c_str(), &dst_addr.sin_addr);
    {
        std::lock_guard<std::mutex> l(send_mutex_);
        send_buffer_.emplace(data, dst_addr);
    }
    send_cv_.notify_one();
}

std::pair<std::vector<uint8_t>, std::pair<std::string, int>> BufferedSocket::recvfrom(int bufsize) {
    std::unique_lock<std::mutex> l(recv_mutex_);
    if (!recv_cv_.wait_for(l, std::chrono::milliseconds(int(timeout_ * 1000)), [&]{ return !receive_buffer_.empty(); }))
        throw SocketTimeout("recvfrom timeout expired");
    auto p = std::move(receive_buffer_.front());
    receive_buffer_.pop();
    std::vector<uint8_t>& data = p.first;
    if (data.size() > (size_t)bufsize)
        data.resize(bufsize);
    std::string sender_ip(INET_ADDRSTRLEN, '\0');
    const char* result = inet_ntop(AF_INET, &p.second.sin_addr, &sender_ip[0], INET_ADDRSTRLEN);
    if (!result)
        throw std::runtime_error("inet_ntop failed");
    sender_ip.resize(std::min(strlen(sender_ip.c_str()), size_t(INET_ADDRSTRLEN)));
    int sender_port = ntohs(p.second.sin_port);
    return {std::move(data), {sender_ip, sender_port}};
}

void BufferedSocket::settimeout(double timeout_sec) {
    timeout_ = timeout_sec;
    DWORD tv = (DWORD)(timeout_ * 1000);
    setsockopt(sock_, SOL_SOCKET, SO_RCVTIMEO, (const char*)&tv, sizeof(tv));
}

int BufferedSocket::get_received_count() {
    std::lock_guard<std::mutex> l(recv_mutex_);
    return (int)receive_buffer_.size();
}
