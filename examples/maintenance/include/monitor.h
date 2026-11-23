#pragma once
#include "device.h"

namespace plant {
struct Alarm {
    std::string deviceId;
    FaultCode fault;
    std::string action;
};

// 同名类只属于网络连接，不能自动解释为现场设备。
namespace network { struct Device { std::string address; }; }

class TemperatureMonitor {
public:
    explicit TemperatureMonitor(double thresholdC);
    // 温度超过阈值时生成告警；离线传感器走单独分支。
    Alarm evaluate(Device& device, const Reading& temperature);
    bool exceeds(double value) const;
    bool exceeds(const Reading& reading) const;
private:
    double thresholdC_;
};
}
