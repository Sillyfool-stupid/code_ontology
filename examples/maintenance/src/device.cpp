#include "device.h"
#include <utility>
namespace plant {
Device::Device(std::string id) : id_(std::move(id)) {}
void Device::stop() { state_ = DeviceState::Stopped; }
std::string Device::id() const { return id_; }
DeviceState Device::state() const { return state_; }
void Device::setState(DeviceState state) { state_ = state; }
Pump::Pump(std::string id) : Device(std::move(id)) {}
void Pump::stop() { Device::stop(); }
void Pump::inspect(const Component& component) { (void)component; }
}
