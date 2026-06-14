# Changelog

All notable changes to NDI Multicam Recorder will be documented in this file.

## [0.1.0-beta.1] - 2026-06-14

### Added
- Multi-source NDI stream recording with per-source recording controls
- Per-source recording overrides for flexible recording workflows
- OSC listener on startup when enabled for remote control
- Per-stream settings gear button for quick option access
- Optional encoder timing diagnostics via MCR_ENC_STATS environment variable

### Fixed
- **Critical:** Fixed AV desync, periodic stutter, and slow stop issues in recordings
- Audio/video sync now maintains proper alignment with sample-accurate timing
- Improved encoder queue handling to ride out transient system stalls
- True constant-frame-rate output at the exact source rate (29.97, 59.94, 25, etc.)

### Changed
- Encoder logs now tagged with source name for better diagnostics
- Improved option visibility in the UI
- Enhanced encoder queue sizing for better stability

### Technical
- Video PTS now uses source frame index on rational time base for true CFR
- Encoder thread properly handles frame drops without breaking timeline or audio sync
- CPU-side format conversion optimized for multi-stream scenarios
