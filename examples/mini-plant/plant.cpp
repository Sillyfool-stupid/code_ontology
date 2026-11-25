#include "plant.h"

namespace mini_plant {

Limits defaultLimits() {
    return {85.0, 150.0, 7.1, 500};
}

Pump::Pump(int equipmentId, Limits limits)
    : equipmentId_(equipmentId),
      limits_(limits),
      lastReading_{20.0, 200.0, 1.0, true},
      state_(DeviceState::Stopped),
      alarm_{AlarmCode::None, false, false, 0},
      task_{equipmentId, AlarmCode::None, false, false},
      operatingHours_(0),
      lastServiceHour_(0) {
}

bool Pump::start() {
    if (state_ != DeviceState::Stopped) {
        return false;
    }
    if (alarm_.active || task_.scheduled) {
        return false;
    }
    if (!lastReading_.sensorOnline) {
        raiseAlarm(AlarmCode::SensorOffline);
        return false;
    }
    state_ = DeviceState::Running;
    return true;
}

void Pump::stop() {
    if (state_ == DeviceState::Running) {
        state_ = DeviceState::Stopped;
    }
}

void Pump::raiseAlarm(AlarmCode code) {
    if (!alarm_.active || alarm_.code != code) {
        ++alarm_.occurrences;
        alarm_.acknowledged = false;
    }
    alarm_.code = code;
    alarm_.active = true;
}

void Pump::clearAlarm() {
    alarm_.code = AlarmCode::None;
    alarm_.active = false;
    alarm_.acknowledged = false;
}

void Pump::update(const Reading& reading) {
    lastReading_ = reading;
    if (state_ != DeviceState::Running) {
        return;
    }
    if (!reading.sensorOnline) {
        raiseAlarm(AlarmCode::SensorOffline);
        state_ = DeviceState::Faulted;
        return;
    }
    if (reading.temperatureC > limits_.maxTemperatureC) {
        raiseAlarm(AlarmCode::OverTemperature);
        state_ = DeviceState::Faulted;
        return;
    }
    if (reading.oilPressureKpa < limits_.minOilPressureKpa) {
        raiseAlarm(AlarmCode::LowOilPressure);
        state_ = DeviceState::Faulted;
        return;
    }
    if (reading.vibrationMmS > limits_.maxVibrationMmS) {
        raiseAlarm(AlarmCode::ExcessVibration);
        state_ = DeviceState::Faulted;
    }
}

void Pump::tickHours(int hours) {
    if (state_ != DeviceState::Running || hours <= 0) {
        return;
    }
    operatingHours_ += hours;
    if (operatingHours_ - lastServiceHour_ >= limits_.serviceIntervalHours) {
        raiseAlarm(AlarmCode::ServiceDue);
    }
}

bool Pump::acknowledgeAlarm() {
    if (!alarm_.active) {
        return false;
    }
    alarm_.acknowledged = true;
    return true;
}

bool Pump::resetFault() {
    if (state_ != DeviceState::Faulted || !alarm_.acknowledged) {
        return false;
    }
    if (!lastReading_.sensorOnline) {
        return false;
    }
    if (lastReading_.temperatureC > limits_.maxTemperatureC ||
        lastReading_.oilPressureKpa < limits_.minOilPressureKpa ||
        lastReading_.vibrationMmS > limits_.maxVibrationMmS) {
        return false;
    }
    clearAlarm();
    state_ = DeviceState::Stopped;
    return true;
}

bool Pump::scheduleMaintenance() {
    if (!alarm_.active || !alarm_.acknowledged || task_.scheduled) {
        return false;
    }
    if (state_ == DeviceState::Running) {
        stop();
    }
    task_ = {equipmentId_, alarm_.code, true, false};
    state_ = DeviceState::Maintenance;
    return true;
}

bool Pump::completeMaintenance() {
    if (state_ != DeviceState::Maintenance || !task_.scheduled) {
        return false;
    }
    if (!lastReading_.sensorOnline ||
        lastReading_.temperatureC > limits_.maxTemperatureC ||
        lastReading_.oilPressureKpa < limits_.minOilPressureKpa ||
        lastReading_.vibrationMmS > limits_.maxVibrationMmS) {
        return false;
    }
    task_.completed = true;
    task_.scheduled = false;
    lastServiceHour_ = operatingHours_;
    clearAlarm();
    state_ = DeviceState::Stopped;
    return true;
}

DeviceState Pump::state() const {
    return state_;
}

Alarm Pump::alarm() const {
    return alarm_;
}

MaintenanceTask Pump::task() const {
    return task_;
}

int Pump::operatingHours() const {
    return operatingHours_;
}

}  // namespace mini_plant
