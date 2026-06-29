#pragma once

#include <queue>
#include <mutex>
#include <condition_variable>
#include <thread>
#include <string>
#include <utility>
#include <stdexcept>
#include <cstring>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <atomic>
#include <algorithm>

#undef min // defined in minwindef.h; conflicts with std::min from algorithm

#pragma comment(lib, "Ws2_32.lib")

// In case of standalone use (without Python binding), ensure Winsock is initialized by calling WSAStartup

namespace buffered_socket {

class SocketTimeout : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

template<typename Container>
class BufferedSocket {
public:
    // Default OS UDP receive buffer (16 MiB) - large enough to absorb a burst of
    // packets while the consumer is briefly blocked (e.g. the trigger flush).
    static constexpr int kDefaultRecvBufferBytes = 16 * 1024 * 1024;
    BufferedSocket(int max_size = 4096, const std::string& name = "BufferedSocket")
        : max_size_(max_size), running_(false), sock_(INVALID_SOCKET),
          timeout_(1.0), received_count_(0), name_(name),
          recv_buffer_bytes_(kDefaultRecvBufferBytes)
    {}

    ~BufferedSocket() {
        close();
    }

    std::pair<std::string, int> bind(int port, bool use_my_ip = false, const std::string& device_ip = "192.168.1.100", int device_port = 9999) {
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
        apply_recv_buffer();

        start();

        return {local_ip, port};
    }

    void attach(SOCKET sock) {
        close();
        std::lock_guard<std::mutex> lock(sock_mutex_);
        sock_ = sock;
        DWORD tv = (DWORD)(timeout_ * 1000);
        setsockopt(sock_, SOL_SOCKET, SO_RCVTIMEO, (const char*)&tv, sizeof(tv));
        apply_recv_buffer();
        start();
    }

    void close() {
        running_ = false;
        send_cv_.notify_all();
        recv_cv_.notify_all();

        {
            std::lock_guard<std::mutex> lock(sock_mutex_);
            if (sock_ != INVALID_SOCKET) {
                closesocket(sock_);
                sock_ = INVALID_SOCKET;
            }
        }

        if (listener_thread_.joinable()) listener_thread_.join();
        if (sender_thread_.joinable()) sender_thread_.join();
        // Empty buffers:
        {
            std::lock_guard<std::mutex> l(recv_mutex_);
            std::queue<std::pair<Container, sockaddr_in>>().swap(receive_buffer_);
        }
        {
            std::lock_guard<std::mutex> l(send_mutex_);
            std::queue<std::pair<Container, sockaddr_in>>().swap(send_buffer_);
        }
    }

    void sendto(const Container& data, const std::string& ip, int port) {
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

    std::pair<Container, std::pair<std::string, int>> recvfrom(int bufsize) {
        std::unique_lock<std::mutex> l(recv_mutex_);
        if (!recv_cv_.wait_for(l, std::chrono::milliseconds(int(timeout_ * 1000)), [&]{ return !receive_buffer_.empty(); }))
            throw SocketTimeout("recvfrom timeout expired");
        auto p = std::move(receive_buffer_.front());
        receive_buffer_.pop();
        l.unlock();
        Container& data = p.first;
        if (data.size() > (size_t)bufsize)
            data.resize(bufsize);
        std::string sender_ip(INET_ADDRSTRLEN, '\0');
        if (!inet_ntop(AF_INET, &p.second.sin_addr, &sender_ip[0], INET_ADDRSTRLEN))
            throw std::runtime_error("inet_ntop failed");
        sender_ip.resize(std::min(strlen(sender_ip.c_str()), size_t(INET_ADDRSTRLEN)));
        return std::make_pair(std::move(data), std::make_pair(std::move(sender_ip), ntohs(p.second.sin_port)));
    }

    void settimeout(double timeout_sec) {
        timeout_ = timeout_sec;
        DWORD tv = (DWORD)(timeout_ * 1000);
        std::lock_guard<std::mutex> lock(sock_mutex_);
        if (sock_ != INVALID_SOCKET) {
            setsockopt(sock_, SOL_SOCKET, SO_RCVTIMEO, (const char*)&tv, sizeof(tv));
        }
    }
    
    int get_received_count() {
        std::lock_guard<std::mutex> l(recv_mutex_);
        return received_count_;
    }

    int get_buffered_items_count() {
        std::lock_guard<std::mutex> l(recv_mutex_);
        return (int)receive_buffer_.size();
    }

    // Request an OS UDP receive buffer of `bytes` and return the value the OS reports.
    // A large buffer prevents packet loss when the consumer briefly stalls (e.g. the
    // synchronous trigger flush re-parses many buffered packets in one burst).
    int set_recv_buffer(int bytes) {
        recv_buffer_bytes_ = bytes;
        std::lock_guard<std::mutex> lock(sock_mutex_);
        apply_recv_buffer();
        if (sock_ == INVALID_SOCKET) return recv_buffer_bytes_;
        int actual = 0;
        int len = sizeof(actual);
        getsockopt(sock_, SOL_SOCKET, SO_RCVBUF, (char*)&actual, &len);
        return actual;
    }

    SOCKET sock_;
    std::atomic<bool> running_;

private:
    // Apply recv_buffer_bytes_ as SO_RCVBUF. Caller must hold sock_mutex_.
    void apply_recv_buffer() {
        if (sock_ != INVALID_SOCKET && recv_buffer_bytes_ > 0) {
            int n = recv_buffer_bytes_;
            setsockopt(sock_, SOL_SOCKET, SO_RCVBUF, (const char*)&n, sizeof(n));
        }
    }

    void listen_loop() {
        while (running_) {
            SOCKET sock_local = INVALID_SOCKET;
            {
                std::lock_guard<std::mutex> lock(sock_mutex_);
                sock_local = sock_;
            }

            if (sock_local == INVALID_SOCKET) {
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
                continue;
            }
            
            sockaddr_in src_addr{};
            int addrlen = sizeof(src_addr);
            Container buffer;
            buffer.resize(max_size_);
            int ret = ::recvfrom(sock_local, (char*)buffer.data(), max_size_, 0, (sockaddr*)&src_addr, &addrlen);
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

    void send_loop() {
        while (running_) {
            std::pair<Container, sockaddr_in> item;
            {
                std::unique_lock<std::mutex> l(send_mutex_);
                send_cv_.wait_for(l, std::chrono::milliseconds(100), [&]{ return !send_buffer_.empty() || !running_; });
                if (!running_) break;
                if (send_buffer_.empty()) continue;
                item = std::move(send_buffer_.front());
                send_buffer_.pop();
            }

            SOCKET sock_local = INVALID_SOCKET;
            {
                std::lock_guard<std::mutex> lock(sock_mutex_);
                sock_local = sock_;
            }

            if (sock_local == INVALID_SOCKET) {
                continue;
            }

            ::sendto(sock_local, (const char*)item.first.data(), (int)item.first.size(),
                   0, (sockaddr*)&item.second, sizeof(item.second));
        }
    }

    void start() {
        if (sock_ == INVALID_SOCKET) throw std::runtime_error("Call bind() first.");
        running_ = true;
        listener_thread_ = std::thread(&BufferedSocket::listen_loop, this);
        sender_thread_ = std::thread(&BufferedSocket::send_loop, this);
    }

    int max_size_;
    double timeout_;
    std::mutex sock_mutex_;
    std::thread listener_thread_;
    std::thread sender_thread_;

    std::queue<std::pair<Container, sockaddr_in>> receive_buffer_;
    std::mutex recv_mutex_;
    std::condition_variable recv_cv_;

    std::queue<std::pair<Container, sockaddr_in>> send_buffer_;
    std::mutex send_mutex_;
    std::condition_variable send_cv_;

    int received_count_;
    std::string name_;
    int recv_buffer_bytes_;
};

} // namespace bufferred_socket
