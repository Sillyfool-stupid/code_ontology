#pragma once

namespace mini_plant {

enum class DeviceState {
    Stopped,
    Running,
    Faulted,
    Maintenance
};

enum class AlarmCode {
    None,
    SensorOffline,
    OverTemperature,
    LowOilPressure,
    ExcessVibration,
    ServiceDue
};

struct Reading {
    double temperatureC;
    double oilPressureKpa;
    double vibrationMmS;
    bool sensorOnline;
};

struct Limits {
    double maxTemperatureC;
    double minOilPressureKpa;
    double maxVibrationMmS;
    int serviceIntervalHours;
};

struct Alarm {
    AlarmCode code;
    bool active;
    bool acknowledged;
    int occurrences;
};

struct MaintenanceTask {
    int equipmentId;
    AlarmCode reason;
    bool scheduled;
    bool completed;
};

class Pump {
public:
    explicit Pump(int equipmentId, Limits limits);
    bool start();
    void stop();
    void update(const Reading& reading);
    void tickHours(int hours);
    bool acknowledgeAlarm();
    bool resetFault();
    bool scheduleMaintenance();
    bool completeMaintenance();
    DeviceState state() const;
    Alarm alarm() const;
    MaintenanceTask task() const;
    int operatingHours() const;

private:
    void raiseAlarm(AlarmCode code);
    void clearAlarm();
    int equipmentId_;
    Limits limits_;
    Reading lastReading_;
    DeviceState state_;
    Alarm alarm_;
    MaintenanceTask task_;
    int operatingHours_;
    int lastServiceHour_;
};

Limits defaultLimits();
int runDemonstration();

}  // namespace mini_plant
