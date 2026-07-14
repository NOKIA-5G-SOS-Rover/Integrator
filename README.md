# NOKIA 5G SOS Rover - Integrator

**Maintainer:** András-Károly Teodorovits
**Date:** July 2026

Central repository for system architecture and integration protocols.

## Structure
* `/docs/architecture/README.md`: System overview and component definitions.
* `/docs/architecture/diagrams/`: PlantUML source code for structural (BCE) and behavioral (Sequence) models.
* `/docs/architecture/img/`: Rendered SVG diagrams.

## Active Tasks
* **Issue #1: Define Architecture** -> **Completed**. Hardware boundaries and trigger sequences mapped.
* **Issue #2: Implement GitHub Actions** -> **Pending**. Setting up CI/CD pipelines across all repositories 
1. for `Integrator` - upon modifying a puml file and commiting + pushing the change => updating folder .svg and README
2. for `AI-ML`
3. for `Embedded`
4. for `Cloud`
5. for `Frontend`

## Developer Setup
1. Clone all 5 project repositories into a single parent folder.
2. Open the parent folder in VS Code using **File -> Add Folder to Workspace**.
3. Install the **PlantUML** extension to view and edit diagram source files.