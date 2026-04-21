<?xml version='1.0' encoding='UTF-8'?>
<esdl:EnergySystem xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:esdl="http://www.tno.nl/esdl" id="bdfae9dc-05bb-4ff0-9136-6ba6b10fc8f2" name="Datacenter_BESS_Scenario" description="Datacenter with BESS and grid connection">
  <instance xsi:type="esdl:Instance" id="0a21a939-6970-4a7c-a199-89271f5ef2b2" name="scenario_instance">
    <area xsi:type="esdl:Area" name="Datacenter_Site" id="92f68048-eff7-42a6-9f15-496b3e59189a">
      <asset xsi:type="esdl:ElectricityNetwork" name="Site LV Network" id="c49ccd77-8e13-4897-b01f-62722f91de45">
        <port xsi:type="esdl:OutPort" connectedTo="bc111594-c217-4cf6-bd8b-5886d9b6999e" name="net_to_datacenter" id="4683a640-c5b3-4bd8-9df1-3beb450e06c5"/>
        <port xsi:type="esdl:OutPort" connectedTo="ab275172-2bb1-4f18-8b17-85930333814b" name="net_to_bess" id="50985514-8e75-4b31-b434-b6a53f022ae6"/>
        <port xsi:type="esdl:InPort" name="net_from_bess" id="e3cf091e-0644-4994-9757-7c572f8e0619" connectedTo="35517ade-1522-409e-906f-9c9111c043ee"/>
        <port xsi:type="esdl:OutPort" connectedTo="15f491cc-d394-4de7-8d45-d67625f785eb" name="net_to_grid" id="9f7ec58a-78e3-43cf-9ab4-882fb9d8fe0b"/>
        <port xsi:type="esdl:InPort" name="net_from_grid" id="2b2e47b0-f600-4dce-a999-f6936ee3ab00" connectedTo="cdaee452-44aa-4aa5-a64c-0456a85b8e56"/>
        <port xsi:type="esdl:InPort" name="net_from_backup_generator" id="fb2e5d95-5531-4801-b363-4badfe308653" connectedTo="3b3baa87-9f60-4e33-8328-47bd5f84acba"/>
        <port xsi:type="esdl:InPort" name="net_from_local_res" id="e5f92b2d-5597-4505-aee4-138c63959815" connectedTo="9ef0498d-b456-409a-9dc4-d601945db631"/>
      </asset>
      <asset xsi:type="esdl:ElectricityDemand" name="Datacenter Load" power="4000000.0" id="fd630fcf-825d-4a92-8445-cdc681d81846" powerFactor="0.95">
        <port xsi:type="esdl:InPort" name="dc_in" id="bc111594-c217-4cf6-bd8b-5886d9b6999e" connectedTo="4683a640-c5b3-4bd8-9df1-3beb450e06c5"/>
      </asset>
      <asset xsi:type="esdl:Battery" name="Datacenter BESS" maxDischargeRate="4000000.0" maxChargeRate="4000000.0" dischargeEfficiency="0.95" id="fc7cc310-ae44-4b12-a59f-719cae65cde3" chargeEfficiency="0.95" capacity="10000.0">
        <port xsi:type="esdl:InPort" name="bess_in" id="ab275172-2bb1-4f18-8b17-85930333814b" connectedTo="50985514-8e75-4b31-b434-b6a53f022ae6"/>
        <port xsi:type="esdl:OutPort" connectedTo="e3cf091e-0644-4994-9757-7c572f8e0619" name="bess_out" id="35517ade-1522-409e-906f-9c9111c043ee"/>
      </asset>
      <asset xsi:type="esdl:PowerPlant" name="Grid Connection" id="e4a6bb69-dd94-43df-95ec-58a1b63ffc75" minLoad="-5000000" power="5000000.0" efficiency="1.0">
        <port xsi:type="esdl:InPort" name="grid_in" id="15f491cc-d394-4de7-8d45-d67625f785eb" connectedTo="9f7ec58a-78e3-43cf-9ab4-882fb9d8fe0b"/>
        <port xsi:type="esdl:OutPort" connectedTo="2b2e47b0-f600-4dce-a999-f6936ee3ab00" name="grid_out" id="cdaee452-44aa-4aa5-a64c-0456a85b8e56"/>
      </asset>
      <asset xsi:type="esdl:GasProducer" name="Backup Generator" power="5000000.0" id="a35ab950-ebf3-4255-9e6b-220170093297">
        <KPIs xsi:type="esdl:KPIs" id="dc12ef30-f6b6-4986-9695-6beca534b2ab">
          <kpi xsi:type="esdl:DoubleKPI" id="5b4a2bca-566d-4e80-a7f2-9b70b5f285c0" value="60.0" name="startup_delay_s"/>
        </KPIs>
        <port xsi:type="esdl:OutPort" connectedTo="fb2e5d95-5531-4801-b363-4badfe308653" name="gen_out" id="3b3baa87-9f60-4e33-8328-47bd5f84acba"/>
      </asset>
      <asset xsi:type="esdl:PVInstallation" name="Local RES" power="1000000.0" inverterEfficiency="0.98" id="9f36816c-6d74-4035-9e51-b3fbeace6a1d" angle="35" surfaceArea="5000" panelEfficiency="0.2" orientation="180">
        <port xsi:type="esdl:OutPort" connectedTo="e5f92b2d-5597-4505-aee4-138c63959815" name="res_out" id="9ef0498d-b456-409a-9dc4-d601945db631"/>
      </asset>
    </area>
  </instance>
</esdl:EnergySystem>
