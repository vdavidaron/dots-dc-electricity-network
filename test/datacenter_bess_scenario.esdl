<?xml version='1.0' encoding='UTF-8'?>
<esdl:EnergySystem xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:esdl="http://www.tno.nl/esdl" name="Datacenter_BESS_Scenario" description="Datacenter with BESS and grid connection" id="b9249dfb-4ddf-463d-a09a-1bd3a6fcb451">
  <instance xsi:type="esdl:Instance" name="scenario_instance" id="0ba8d072-de20-42bf-a6a8-ed15770be494">
    <area xsi:type="esdl:Area" id="308c2119-c22c-4797-b33b-2b3c12264235" name="Datacenter_Site">
      <asset xsi:type="esdl:ElectricityNetwork" name="Site LV Network" id="8e885285-0de9-45a5-92db-2bb8ddb54172">
        <port xsi:type="esdl:OutPort" id="37856337-6502-4d42-adc6-1ef9a0c6bcea" connectedTo="945dbd55-c6f9-48da-bd19-90b218fbc4ff" name="net_to_datacenter"/>
        <port xsi:type="esdl:OutPort" id="1729d096-5adb-4f57-88a8-59e0aa3c523e" connectedTo="4b7191e9-90b4-49fa-920d-f44a09072731" name="net_to_bess"/>
        <port xsi:type="esdl:InPort" id="70f42a4a-b00d-48ae-b55e-20304ba30845" connectedTo="18ce9fd5-54a4-4370-a833-53c857e6e1f5" name="net_from_bess"/>
        <port xsi:type="esdl:OutPort" id="fcc248e2-d275-418e-b239-8f8962fee215" connectedTo="d32c543c-30db-41c4-8551-ad3c91345ec6" name="net_to_grid"/>
        <port xsi:type="esdl:InPort" id="596ce376-b495-42c7-a38b-705f1e3cc06e" connectedTo="b3e1bcd0-2102-4bfb-afa0-2b188442247e" name="net_from_grid"/>
        <port xsi:type="esdl:InPort" id="54ae5a4b-1c32-484d-9dc3-d3e7729713c5" connectedTo="590052a6-f634-41dc-93ab-0f57e92ed58c" name="net_from_backup_generator"/>
        <port xsi:type="esdl:InPort" id="1198de97-8dbe-44f8-bf84-bced5ad58536" connectedTo="489205d3-aa93-4eee-be46-35de24f8833e" name="net_from_local_res"/>
      </asset>
      <asset xsi:type="esdl:ElectricityDemand" id="21f2f015-a82c-4dd2-9c63-03ae695947a9" powerFactor="0.95" name="Datacenter Load" power="4000000.0">
        <port xsi:type="esdl:InPort" name="dc_in" id="945dbd55-c6f9-48da-bd19-90b218fbc4ff" connectedTo="37856337-6502-4d42-adc6-1ef9a0c6bcea">
          <profile xsi:type="esdl:InfluxDBProfile" database="GO-e" host="influxdb" measurement="historical_datacenter_demand" id="893fd290-0e22-4eeb-85ff-467b5d9b8401" filters="name='Datacenter Load'" port="8086" field="Demand_W"/>
        </port>
      </asset>
      <asset xsi:type="esdl:Battery" dischargeEfficiency="0.95" id="5767c6bb-9700-44c5-9c4c-a6d411f078df" chargeEfficiency="0.95" capacity="4000000.0" maxDischargeRate="4000000.0" name="Datacenter BESS" maxChargeRate="4000000.0">
        <port xsi:type="esdl:InPort" id="4b7191e9-90b4-49fa-920d-f44a09072731" connectedTo="1729d096-5adb-4f57-88a8-59e0aa3c523e" name="bess_in"/>
        <port xsi:type="esdl:OutPort" id="18ce9fd5-54a4-4370-a833-53c857e6e1f5" connectedTo="70f42a4a-b00d-48ae-b55e-20304ba30845" name="bess_out"/>
      </asset>
      <asset xsi:type="esdl:PowerPlant" id="2d16b31c-6987-4266-b5b7-b652ac9b0610" minLoad="-75000000" power="75000000.0" name="Grid Connection" efficiency="1.0">
        <port xsi:type="esdl:InPort" name="grid_in" id="d32c543c-30db-41c4-8551-ad3c91345ec6" connectedTo="fcc248e2-d275-418e-b239-8f8962fee215">
          <profile xsi:type="esdl:InfluxDBProfile" database="GO-e" host="influxdb" measurement="transformer_background" id="ee545567-962b-4669-9ca6-db6e7a9c4b1c" filters="name='Grid Connection'" port="8086" field="background_w"/>
          <profile xsi:type="esdl:InfluxDBProfile" database="GO-e" host="influxdb" measurement="carbon_intensity" id="aac1d811-def7-48f1-ace9-4576395a3dd0" filters="name='Grid Connection'" port="8086" field="carbon_intensity"/>
        </port>
        <port xsi:type="esdl:OutPort" id="b3e1bcd0-2102-4bfb-afa0-2b188442247e" connectedTo="596ce376-b495-42c7-a38b-705f1e3cc06e" name="grid_out"/>
      </asset>
      <asset xsi:type="esdl:GasProducer" id="4b4c5d3c-26f0-418b-a438-539731736293" name="Backup Generator" power="5000000.0">
        <port xsi:type="esdl:OutPort" id="590052a6-f634-41dc-93ab-0f57e92ed58c" connectedTo="54ae5a4b-1c32-484d-9dc3-d3e7729713c5" name="gen_out"/>
        <KPIs xsi:type="esdl:KPIs" id="57e18022-bf24-4bc5-979e-d4e232ffb71e">
          <kpi xsi:type="esdl:DoubleKPI" name="startup_delay_s" id="b4323fcf-6dcf-41fc-8d69-348e89ec451d" value="60.0"/>
        </KPIs>
      </asset>
      <asset xsi:type="esdl:PVInstallation" inverterEfficiency="0.98" id="0d3051a9-f4ec-467b-9321-0425c1ba8cd6" angle="35" panelEfficiency="0.2" name="Local RES" surfaceArea="5000" power="1000000.0" orientation="180">
        <port xsi:type="esdl:OutPort" name="res_out" id="489205d3-aa93-4eee-be46-35de24f8833e" connectedTo="1198de97-8dbe-44f8-bf84-bced5ad58536">
          <profile xsi:type="esdl:InfluxDBProfile" database="GO-e" host="influxdb" measurement="historical_solar_irradiance" id="509be6fd-51df-4cd6-b761-8795509fffb8" filters="name='Local RES'" port="8086" field="Irradiance_W_m2"/>
        </port>
      </asset>
    </area>
  </instance>
</esdl:EnergySystem>
