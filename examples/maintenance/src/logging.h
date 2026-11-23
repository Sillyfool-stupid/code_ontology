#pragma once
#include <string>
// 基础设施负例：不是业务告警中心。
namespace infra {
class Logger {
public:
    void warning(const std::string& message) { (void)message; }
};
template <typename T> class Cache {
public:
    void put(const T& value) { value_ = value; }
private:
    T value_{};
};
}
