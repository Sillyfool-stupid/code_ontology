#pragma once
#include <string>

namespace plant {
// 设备状态用于运维判断，不代表现场已经发生状态变化。
enum class DeviceState { Running, Warning, Stopped };
enum class FaultCode { None, OverTemperature, SensorOffline };

// 设备部件，例如轴承和电机。
struct Component {
    std::string id;
    std::string name;
};

// 监测读数：数值、单位以及传感器在线状态。
struct Reading {
    double value;
    std::string unit;
    bool online;
};

class Device {
public:
    explicit Device(std::string id);
    virtual ~Device() = default;
    virtual void stop();
    std::string id() const;
    DeviceState state() const;
    void setState(DeviceState state);
private:
    std::string id_;
    DeviceState state_ = DeviceState::Running;
};

class Pump : public Device {
public:
    explicit Pump(std::string id);
    void stop() override;
    void inspect(const Component& component);
};
}
