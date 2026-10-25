# DeepPrint Footprints

These footprints are ready to render with:

```powershell
python DeepPrint/deepprint.py --list-personas
python DeepPrint/deepprint.py --deploy <persona_name> --dry-run
python DeepPrint/deepprint.py --deploy <persona_name>
```

`power_plant` also demonstrates prompt-driven hostnames and banner values.

## Deployable Templates

- `airport_ops` - airport baggage handling and terminal operations
- `cloud_storage` - Nextcloud-style file cloud with PHP and Node.js web stack
- `datacenter_core` - colocation power and network core environment
- `food_processing` - packaging line and refrigeration controls
- `hospital_imaging` - radiology and biomedical device network
- `maritime_port` - port crane control and quay operations
- `mining_operations` - haulage dispatch and concentrator plant controls
- `oil_gas_pipeline` - compressor station and pipeline telemetry
- `pharma_cleanroom` - regulated cleanroom HVAC and batch systems
- `power_plant` - generation station and turbine hall controls
- `rail_operations` - dispatch, signaling, and interlocking systems
- `retail_payment` - store back office and payment processing environment
- `semiconductor_fab` - cleanroom tool support and process utility controls
- `smart_warehouse` - fulfillment center conveyor and sorter network
- `telecom_edge` - edge POP power plant and peering segment
- `university_research` - applied physics lab instrumentation zone
- `utility_substation` - transmission substation relay and breaker controls
- `water_treatment` - chlorination and plant SCADA environment

The `cloud_storage` footprint is a web-deception persona: it renders against
its own base template (`templates/tpot-web.yml`) and adds a `webdecoy`
service that serves a Nextcloud-style login experience fingerprinting as
both PHP and Node.js. See `cloud_storage/README.md` for its route table,
WAF-fronting guidance, and log locations.
