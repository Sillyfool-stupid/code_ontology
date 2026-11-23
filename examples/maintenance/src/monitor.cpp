#include "monitor.h"
namespace plant {
TemperatureMonitor::TemperatureMonitor(double thresholdC) : thresholdC_(thresholdC) {}
bool TemperatureMonitor::exceeds(double value) const { return value > thresholdC_; }
bool TemperatureMonitor::exceeds(const Reading& reading) const { return exceeds(reading.value); }
Alarm TemperatureMonitor::evaluate(Device& device, const Reading& temperature) {
    if (!temperature.online) {
        device.setState(DeviceState::Warning);
        return {device.id(), FaultCode::SensorOffline, "inspect sensor"};
    }
    if (exceeds(temperature)) {
        device.setState(DeviceState::Warning);
#ifdef ENABLE_REMOTE
        device.stop();
#endif
        return {device.id(), FaultCode::OverTemperature, "cool down and inspect bearing"};
    }
    return {device.id(), FaultCode::None, "none"};
}
}
