# 5G SOS Rover - System Architecture

**Author:** András-Károly Teodorovits  
**Date:** July 2026  
**Status:** Initial Draft (Issue #1)

## Overview
This document defines the high-level software architecture for the 5G SOS Rover. To ensure absolute clarity and adhere to UML standards, our documentation strictly separates structural modeling from temporal/behavioral modeling. 

---

## 1. Structural Architecture (BCE)

The diagram below represents the **Structural Architecture** using the Robustness (Boundary-Control-Entity) pattern. It defines the components across our dual-processing hardware (MPU/Linux + MCU/STM32) and their permitted communication channels.

![Rover Structural Architecture](./img/rover_bce.svg)

### Component Breakdown

#### MPU Partition (Qualcomm Debian - AI & Comms)
Responsible for heavy computation, edge AI inference, and network routing.
* **Boundaries:**
  * `Video Camera Interface`: Captures the raw video stream.
  * `5G Module Adapter`: Handles serial/network communication with the standalone 5G hardware.
* **Controls:**
  * `AI Vision Controller`: Executes computer vision models for person detection.
  * `Main Navigation Controller`: Manages high-level routing and decision-making logic.
* **Entities:**
  * `TargetState`: Data model containing detected person coordinates and SOS flags.
  * `RoverState`: Data model containing telemetry (e.g., battery level, network strength).

#### MCU Partition (STM32 - Real-Time Control)
Responsible for hard real-time execution and physical hardware interaction.
* **Boundaries:**
  * `Motor Drivers`: Hardware interface to the H-bridges controlling the 4 motors.
* **Controls:**
  * `Motion Controller`: Translates MPU navigation vectors into real-time PWM signals and manages emergency hardware stops.

---

## 2. Behavioral Architecture (Sequence)

The following sequence details the chronological execution of an SOS event. It demonstrates how the MPU parallelizes external network alerts and real-time internal hardware control upon detecting a target.

![SOS Trigger Sequence](./img/sos_trigger_sequence.svg)

### Execution Timeline
1. **Inference:** The `AI Vision Controller` continuously polls video frames and runs local inference.
2. **Event Trigger:** Upon target detection, an alert is routed to the `Navigation Controller`.
3. **Parallel Dispatch:**
   * **Cloud Route:** The navigation brain pushes a JSON payload through the `5G Adapter` to the external `Cloud Dashboard`.
   * **Hardware Route:** Simultaneously, an emergency halt vector is sent over the internal serial interface to the `Motion Controller` (MCU) to immediately drop motor PWM signals.

---

## 3. Integration Protocols

* **MPU <-> MCU Communication:** Handled via an internal serial protocol (packet structure and baud rate to be defined).
* **AI-ML <-> Embedded / Navigation:** The `AI Vision Controller` updates the `TargetState` entity, which the `Navigation Controller` polls to adjust movement vectors.

---
**Changelog:**
* *v0.1 - Initial draft: Added BCE structural definition and SOS behavioral sequence diagrams.*