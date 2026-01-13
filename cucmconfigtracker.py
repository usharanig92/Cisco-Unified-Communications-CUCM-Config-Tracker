"""
This Script tracks the changes in the CUCM system configurations and reports if any changes
are detected. It compares the original and the changed configuration and shows which items
have changed, added or removed. The admin who made the change can then commit it
with the commit message.

The commit command will update the base config with the changed information and emails
the team with the summary of the changes and the committer information along with the
commit message.

In this script I used uv to install the required dependencies on-demand instead of running
in a virtual environment. Specifying the required modules under script block as shown below
will install the specified modules during the script run and do the clean up automatically.
Refer here to know more about uv https://github.com/astral-sh/uv

To run this application using uv, use the command "uv run <script_name> <required_arguments>"

"""

"""
This Script tracks the changes in the CUCM system configurations and reports if any changes
are detected. It compares the original and the changed configuration and shows which items
have changed, added or removed. The admin who made the change can then commit it
with the commit message.

The commit command will update the base config with the changed information and emails
the team with the summary of the changes and the committer information along with the
commit message.

In this script I used uv to install the required dependencies on-demand instead of running
in a virtual environment. Specifying the required modules under script block as shown below
will install the specified modules during the script run and do the clean up automatically.
Refer here to know more about uv https://github.com/astral-sh/uv

To run this application using uv, use the command "uv run <script_name> <required_arguments>"

"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pandas",
#     "numpy>=2.0.0",
#     "lxml",
#     "paramiko",
#     "paramiko-expect",
#     "tabulate",
#     "requests",
#     "zeep",
#     "inquirerpy",
# ]
# ///

import argparse
import csv
import getpass
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from csv import reader
from datetime import datetime
from pathlib import Path
from shutil import copyfile
from typing import Any
from xml.etree import ElementTree as ET

import numpy as np
import pandas as pd
import paramiko
from InquirerPy import inquirer
from lxml import etree
from paramiko import SSHClient
from paramiko_expect import SSHClientInteraction
from requests import Session
from requests.auth import HTTPBasicAuth
from tabulate import tabulate
from zeep.cache import SqliteCache
from zeep.client import Client
from zeep.exceptions import Fault
from zeep.plugins import HistoryPlugin, Plugin
from zeep.settings import Settings
from zeep.transports import Transport


class RequestResponseLoggingPlugin(Plugin):
    """Prints out the CUCM request and response when debug is enabled"""

    def egress(self, envelope, http_headers, operation, binding_options):
        """Prints out the egress request to CUCM, if debug is enabled"""
        # Format the request body as pretty printed XML
        xml = etree.tostring(envelope, pretty_print=True, encoding="unicode")

        print(f"\nRequest\n-------\nHeaders:\n{http_headers}\n\nBody:\n{xml}")

    def ingress(self, envelope, http_headers, operation):
        """Prints out the response from CUCM, if debug is enabled"""
        # Format the response body as pretty printed XML
        xml = etree.tostring(envelope, pretty_print=True, encoding="unicode")

        print(f"\nResponse\n-------\nHeaders:\n{http_headers}\n\nBody:\n{xml}")


class ServerCredentialError(Exception):
    """Raised when the CUCM server returns a 401 error."""

    pass


DEBUG = False


def does_last_response_report_credential_error(history) -> bool:
    """Analyses the response for credential error"""
    return "HTTP Status 401" in ET.tostring(history.last_received["envelope"]).decode()


def get_config_relative_path(which_config, config_relative_path, config_item) -> str:
    path = config_relative_path
    config_path = os.path.join(path, which_config, config_item) + ".csv"
    return config_path


def email(*, cucmpub, email_recipient, subject, body) -> None:
    subprocess.run(
        [
            "swiss",
            "simplemail",
            "-to",
            email_recipient,
            "-body",
            body,
            "-content-type",
            "html",
            "-subject",
            subject,
            "-from",
            cucmpub,
        ]
    )


def compare_running_with_base(config_relative_path, config_item) -> str:
    df1 = pd.read_csv(
        get_config_relative_path("baseconfig", config_relative_path, config_item),
        index_col=False,
    ).replace(np.nan, "")
    df2 = pd.read_csv(
        get_config_relative_path("runningconfig", config_relative_path, config_item),
        index_col=False,
    ).replace(np.nan, "")
    htmldiff = ""
    changed_index = []
    if df2.equals(df1):
        logging.info(f"No changes were detected in {config_item}")
    else:
        try:
            # pandas compare method compares only two dataframes of same number of colummns and rows.
            # This block is used to show changes in the existing rows, not add/remove.
            diff = df1.compare(df2.reset_index(drop=True), keep_equal=True).replace(
                np.nan, ""
            )
            diff = diff.rename(
                columns={"self": "baseconfig", "other": "running_config"}
            )
            changed_index.append((diff.columns.get_level_values(0).to_list())[1::2])
            htmldiff += f" <br />Following parameters have been modified in {config_item}: {changed_index} <br />"
            print(
                f"\n\nFollowing parameters have been modified in the {config_item}: {changed_index} \n"
            )
        except Exception:
            pass
        # left_only merge will show the data present in base config, but missing in running config.
        # Ex: some configurations was removed from CUCM, but not updated in base config.
        baseconfig = df1.merge(df2.reset_index(drop=True), indicator=True, how="left")
        base_config = (baseconfig[baseconfig["_merge"] == "left_only"]).drop(
            columns="_merge"
        )
        # right_only merge will show the data present in running_config, but missing in base config.
        # Ex: some configurations was added in CUCM, but not updated in base config.
        runningconfig = df1.merge(df2, indicator=True, how="right")
        running_config = (runningconfig[runningconfig["_merge"] == "right_only"]).drop(
            columns="_merge"
        )

        # Shape gives tuple of (num_of_rows, num_of_cols).
        # columns will always remains same, it is the predefined parameters that we are pulling from CUCM. check only rows to identify the change was
        # happened in baseconfig or running config.
        if (((base_config.values).shape[0]) != 0) & (
            ((running_config.values).shape[0]) == 0
        ):
            # Base config value is non zero, it means after removing the common items from baseconfig and runningconfig, baseconfig still has some rows,
            # so something was removed in the CUCM, but not updated in the base config.
            base_config_html = base_config.to_html()
            body = f"<br />Changes detected in '{config_item}'. <br /> Below configs have been removed: <br />"
            print(
                body.replace("<br />", "\n"),
                tabulate(
                    base_config, headers=base_config.columns, tablefmt="fancy_grid"
                ),
            )
            htmldiff += body + base_config_html
        elif (((running_config.values).shape[0]) != 0) & (
            ((base_config.values).shape[0]) == 0
        ):
            # similar to the above condition, after merging runningconfig still has some extra rows, it means some config was added in CUCM, but not updated in
            # base config.
            running_config_html = running_config.to_html()
            body = "<br />Changes detected in '{csvname}'. <br /> Below configs have been added: <br />".format(
                csvname=config_item
            )
            print(
                body.replace("<br />", "\n"),
                tabulate(
                    running_config,
                    headers=running_config.columns,
                    tablefmt="fancy_grid",
                ),
            )
            htmldiff += body + running_config_html
        else:
            # This final else statement covers both the scenario of some configs were added/removed and also the existing data was modified.
            index = base_config.columns[0]
            base_config_columns = base_config.columns
            running_config_columns = running_config.columns
            if changed_index:
                if index not in changed_index[0]:
                    base_config_columns = changed_index[0].copy()
                    running_config_columns = changed_index[0].copy()
                    base_config_columns.insert(0, base_config.columns[0])
                    running_config_columns.insert(0, running_config.columns[0])
            base_config_html = (base_config[base_config_columns]).to_html()
            body = (
                f"<br />Base configs and running configs has been modified for '{config_item}'. "
                "<br />Configs in Base Repo: <br />"
            )
            print(
                body.replace("<br />", "\n"),
                tabulate(
                    base_config[base_config_columns],
                    headers=base_config[base_config_columns],
                    tablefmt="fancy_grid",
                ),
                sep="",
            )
            htmldiff += body + base_config_html
            running_config_html = (running_config[running_config_columns]).to_html()
            body = "<br />Configs in Running Config: <br />"
            print(
                body.replace("<br />", "\n"),
                tabulate(
                    running_config[running_config_columns],
                    headers=running_config[running_config_columns],
                    tablefmt="fancy_grid",
                ),
                sep="",
            )
            htmldiff += body + running_config_html
    return htmldiff


def update_runningconfig(
    cucmpub, config_relative_path, config_item, resp, email_recipient
) -> str:
    with open(
        get_config_relative_path("baseconfig", config_relative_path, config_item)
    ) as csvfile:
        csv_reader = reader(csvfile)
        header = next(csv_reader)
    filepath = get_config_relative_path(
        "runningconfig", config_relative_path, config_item
    )
    if os.path.exists(filepath):
        os.remove(filepath)
    with open(filepath, "a+") as file:
        fieldnames = header
        writer = csv.writer(file)
        writer.writerow(fieldnames)
        rowXml = ""
        try:
            rowXml = resp["return"]["row"]
        except Exception as e:
            print("Unable to retrieve anything for " + config_item + str(e))
            pass
        for i in range(0, len(rowXml)):
            data = []
            for j in range(0, len(header)):
                # check if the root element has children, and append the children details.
                if len(rowXml[i][j]):
                    child_values = []
                    for child in rowXml[i][j]:
                        child_values.append(f"{child.tag} - {child.text}")  # pyright: ignore[reportAttributeAccessIssue]
                    data.append(child_values)
                data.append(rowXml[i][j].text)  # pyright: ignore[reportAttributeAccessIssue]
            writer.writerow(data)
    result = compare_running_with_base(config_relative_path, config_item)
    if result:
        date = datetime.now().strftime("%Y_%m_%d")
        subject = f"{date} : Changes made in the call manager has been commited to running-config. Please update base-config"
        body = (
            "Please review and either update base configs or roll back changes in CUCM.<br /><br />"
            f"To commit the change run the below command <br /><br />"
            f"<strong> cucmconfigtracker update_base {config_item} <i> reason_for_change </i> </strong> <br /><br />"
            + result
        )
        email(
            cucmpub=cucmpub,
            email_recipient=email_recipient,
            subject=subject,
            body=body,
        )
    else:
        pass

    return result


def create_service(*, cucmpub, username, password, certroot, wsdl_path) -> tuple:
    wsdl = wsdl_path
    hostname = cucmpub
    host = socket.getfqdn(hostname)
    location = f"https://{host}:8443/axl/"
    binding = "{http://www.cisco.com/AXLAPIService/}AXLAPIBinding"
    history = HistoryPlugin()
    auth_header = password
    session = Session()
    session.verify = certroot
    session.auth = HTTPBasicAuth(username, auth_header)
    settings = Settings(strict=False, xml_huge_tree=True)  # pyright: ignore[reportCallIssue]
    transport = Transport(cache=SqliteCache(), session=session, timeout=20)
    plugins = [RequestResponseLoggingPlugin()] if DEBUG else [history]
    client = Client(wsdl=wsdl, settings=settings, transport=transport, plugins=plugins)
    return client.create_service(binding, location), history


def ssh_connect_output(cucmpub, cucm_cli_username, cucm_cli_password, cmd) -> str:
    hostname = cucmpub
    username = cucm_cli_username
    with SSHClient() as ssh:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        auth_header = cucm_cli_password
        ssh.connect(hostname=hostname, username=username, password=auth_header)
        interact = SSHClientInteraction(ssh, display=False)
        interact.expect("admin:")
        interact.send(cmd)
        interact.expect("admin:")
        output = interact.current_output_clean
        return output


def get_presence_server_high_availability_and_save_in_csv(
    cucmpub,
    cucm_cli_username,
    cucm_cli_password,
    config_relative_path,
) -> None:
    cmd = "utils ha status"
    resp = ssh_connect_output(cucmpub, cucm_cli_username, cucm_cli_password, cmd)
    data = re.findall(
        r"\tName:\s+(\S+).*?State:\s+(\S+).*?Reason:\s+(\S+)", resp, re.DOTALL
    )
    # Create a dataframe
    df = pd.DataFrame(
        data,
        columns=["Name", "State", "Reason"],
    )
    export_path = get_config_relative_path(
        "runningconfig",
        config_relative_path,
        "Imp_High_Availability_Status",
    )
    df.to_csv(export_path, index=False)


def auto_check(
    cucmpub, config_relative_path, service, history, email_recipient
) -> None:
    result = ""
    for template, sql in templates.items():
        try:
            resp = execute_sql_query(service, history, sql)
        except Fault as err:
            if does_last_response_report_credential_error(history):
                raise ServerCredentialError(err)
            else:
                raise
        result += update_runningconfig(
            cucmpub, config_relative_path, template, resp, email_recipient
        )
    if result:
        print("Base and Running configs has been modified")
    else:
        print("No changes Detected")


def update_baseconfig(
    cucmpub, config_relative_path, config_item, username, commit, email_recipient
) -> None:
    source = get_config_relative_path(
        "runningconfig", config_relative_path, config_item
    )
    destination = get_config_relative_path(
        "baseconfig", config_relative_path, config_item
    )
    diff = compare_running_with_base(config_relative_path, config_item)
    if diff:
        body = (
            f"New configs have been committed successfully from the above running config to the base repo <br /><br />"
            f"<strong> commit message </strong> {commit} " + diff
        )
        subject = f"CUCM Configs: Base config updated by {username}"
        email(
            cucmpub=cucmpub,
            email_recipient=email_recipient,
            subject=subject,
            body=body,
        )
        copyfile(source, destination)
        print(
            "New configs have been committed successfully from the above running config to the base repo. "
            f"commit message: {commit}"
        )


def execute_sql_query(service, history, sql) -> Any:
    try:
        resp = service.executeSQLQuery(sql)
    except Fault as err:
        if does_last_response_report_credential_error(history):
            raise ServerCredentialError(err)
        else:
            raise
    return resp


def list_change(
    cucmpub,
    config_relative_path,
    cucm_cli_username,
    cucm_cli_password,
    service,
    history,
    templates,
    email_recipient,
) -> int:
    try:
        auto_check(cucmpub, config_relative_path, service, history, email_recipient)
        resp = service.listChange()
    except Fault as err:
        if does_last_response_report_credential_error(history):
            raise ServerCredentialError(err)
        else:
            print(f"\nZeep error: polling listChange: {err}")
            sys.exit(1)
    print("Initial listChange response:")
    print()
    print(resp)

    queue_id = resp.queueInfo.queueId
    next_start_change_id = resp.queueInfo.nextStartChangeId

    print()
    print("Starting loop to monitor changes...")
    print("(Press Ctrl+C to exit)")
    print()
    print(f"Action doGet? Type{16 * ' '} UUID{32 * ' '} Field{10 * ' '} Value")
    print(f"{6 * '-'} {6 * '-'} {20 * '-'} {36 * '-'} {15 * '-'} {15 * '-'}")

    actions = {"a": "Add", "u": "Update", "r": "Remove"}

    while True:
        start_change_id = {"queueId": queue_id, "_value_1": next_start_change_id}
        object_list = [{"object": list(templates.keys())}]
        change_types = set()
        # Execute the listChange request
        try:
            resp = service.listChange(start_change_id, object_list)

        except Exception as err:
            print(f"\nZeep error: polling listChange: {err}")
            break

        if resp.changes:
            # Loop through each change in the changes list
            for change in resp.changes.change:
                change_types.add(change.type)
                # If there are any items in the changedTags list...
                if change.changedTags:
                    # Loop through each changedTag
                    for x in range(len(change.changedTags.changedTag)):
                        # If this is the first changedTag, print a full line of data...
                        if x == 0:
                            print(
                                actions[change.action].ljust(6, " "),
                                change.doGet.ljust(6, " "),
                                change.type.ljust(20, " "),
                                change.uuid,
                                change.changedTags.changedTag[x].name.ljust(15, " "),
                                change.changedTags.changedTag[x]._value_1,
                            )

                        # otherwise print just the field/value part of the line
                        else:
                            print(
                                71 * " ",
                                change.changedTags.changedTag[x].name.ljust(15, " "),
                                change.changedTags.changedTag[x]._value_1,
                            )

                # otherwise just print the minimum details
                else:
                    print(
                        actions[change.action].ljust(6, " "),
                        change.doGet.ljust(6, " "),
                        change.type.ljust(20, " "),
                        change.uuid,
                    )
            try:
                for change in change_types:
                    response = execute_sql_query(service, history, templates[change])
                    update_runningconfig(
                        cucmpub, config_relative_path, change, response, email_recipient
                    )
            except Exception as e:
                print("Unable to update the running config" + str(e))
                break

        # Update the next highest change Id
        next_start_change_id = resp.queueInfo.nextStartChangeId
        get_presence_server_high_availability_and_save_in_csv(
            cucmpub, cucm_cli_username, cucm_cli_password, config_relative_path
        )
        time.sleep(600)

    # We should not exit from the "while True" loop above unless there is an
    # error.
    return 1


def ucconfig_diff_check(config_relative_path, config_item) -> int:
    diff_items = []
    config_item.append("Imp_High_Availability_Status")
    for item in config_item:
        df1 = pd.read_csv(
            get_config_relative_path("baseconfig", config_relative_path, item)
        ).replace(np.nan, "")
        df2 = pd.read_csv(
            get_config_relative_path("runningconfig", config_relative_path, item)
        ).replace(np.nan, "")
        if df2.equals(df1):
            logging.info(f"No changes were detected in {item}")
        else:
            diff_items.append(item)

    if diff_items:
        print(f"Changes detected for the items {diff_items}")
        return 1
    else:
        return 0


route_pattern_sql = """select n.dnorpattern, rp.name as partition, n.description, n.blockenable, n.patternurgency, eccp.name as externalcallprofile,
                        n.supportoverlapsending, n.outsidedialtone, n.deviceoverride, n.authorizationcoderequired, n.clientcoderequired,
                        ts.name as UseCallingPartysExternalMask, n.callingpartytransformationmask, n.callingpartyprefixdigits,
                        pb.name as calling_line_presentation, pb1.name as calling_name_presentation,nt.name as calling_number_type,
                        np.name as calling_numbering_plan, pb2.name as connected_line_presentaion, pb3.name as connected_name_presentation,
                        ddi.name as discard_digits, n.calledpartytransformationmask, n.prefixdigitsout, nt1.name as called_number_type,
                        np1.name as called_numbering_plan

                        from numplan as n left join routepartition as rp on n.fkroutepartition = rp.pkid
                        left join externalcallcontrolprofile as eccp on n.fkexternalcallcontrolprofile=eccp.pkid left join typestatus as ts on
                        n.tkstatus_usefullyqualcallingpartynum=ts.enum left join typepresentationbit as pb
                        on n.tkpresentationbit_callingline = pb.enum left join typepresentationbit as pb1 on
                        n.tkpresentationbit_callingname = pb1.enum left join typepriofnumber as nt on n.tkpriofnumber_calling = nt.enum
                        left join typenumberingplan as np on  n.tknumberingplan_calling = np.enum left join typepresentationbit as pb2 on
                        n.tkpresentationbit_connectedline = pb2.enum left join typepresentationbit as pb3 on
                        n.tkpresentationbit_connectedname = pb3.enum left join digitdiscardinstruction as ddi on n.fkdigitdiscardinstruction = ddi.pkid
                        left join typepriofnumber as nt1 on n.tkpriofnumber_called = nt1.enum left join typenumberingplan as np1 on n.tknumberingplan_called = np1.enum
                        where tkpatternusage='5' order by dnorpattern"""


translation_pattern_sql = """select n.dnorpattern, rp.name as partition, n.description,cssname.name as css, n.useoriginatorcss, eccp.name as externalcallprofile,
                            n.blockenable, n.patternurgency, n.dontwaitforidtatsubsequenthops, n.routenexthopbycgpn,
                            ts.name as UseCallingPartysExternalMask, n.callingpartytransformationmask, n.callingpartyprefixdigits,
                            pb.name as calling_line_presentation, pb1.name as calling_name_presentation,nt.name as calling_number_type,
                            np.name as calling_numbering_plan, pb2.name as connected_line_presentaion, pb3.name as connected_name_presentation,
                            ddi.name as discard_digits, n.calledpartytransformationmask, n.prefixdigitsout, nt1.name as called_number_type,
                            np1.name as called_numbering_plan  from numplan as n left join routepartition as rp on n.fkroutepartition = rp.pkid
                            left join callingsearchspace as cssname on n.fkcallingsearchspace_translation=cssname.pkid
                            left join externalcallcontrolprofile as eccp on n.fkexternalcallcontrolprofile=eccp.pkid left join typestatus as ts on
                            n.tkstatus_usefullyqualcallingpartynum=ts.enum left join typepresentationbit as pb
                            on n.tkpresentationbit_callingline = pb.enum left join typepresentationbit as pb1 on
                            n.tkpresentationbit_callingname = pb1.enum left join typepriofnumber as nt on n.tkpriofnumber_calling = nt.enum
                            left join typenumberingplan as np on  n.tknumberingplan_calling = np.enum left join typepresentationbit as pb2 on
                            n.tkpresentationbit_connectedline = pb2.enum left join typepresentationbit as pb3 on
                            n.tkpresentationbit_connectedname = pb3.enum left join digitdiscardinstruction as ddi on n.fkdigitdiscardinstruction = ddi.pkid
                            left join typepriofnumber as nt1 on n.tkpriofnumber_called = nt1.enum left join typenumberingplan as np1 on
                            n.tknumberingplan_called = np1.enum where n.tkpatternusage='3' order by n.dnorpattern"""

route_groups_sql = """select rg.name, rgd.deviceselectionorder, d.name as device, da.name as distribution_algorithm from routegroup as rg
                        inner join routegroupdevicemap as rgd on rgd.fkroutegroup=rg.pkid inner join device as d on rgd.fkdevice=d.pkid
                        inner join typedistributealgorithm as da on da.enum = rg.tkdistributealgorithm order by rg.name"""

device_pools_sql = """select dp.name, cmg.name as callmanagergroup,css1.name as css_for_auto_registration, css2.name as adjunct_css,rp.name as revert_priority,msd.name as mra_service_domain,
                      dt.name as datetime,r.name as region, mrl.name as mediaresourcelist, l.name as location,tkc.name as network_locale, srst.name as srst, dp.connectionmonitorduration,
                      tb.name as Single_Button_Barge, tsjal.name as Join_Across_Lines, pl.name as physicallocation, dmg.name as device_mobility_group, wlpg.name as wireless_LAN_group,
                      rg.name as standard_local_route_group,css3.name as device_mobility_css, css4.name as aar_css, css5.name as DeviceMobility_cgpn_transform, css6.name as DeviceMobility_cdpn_transform,
                      geo.name as geolocation,gf.name as geo_location_filter,  dp.nationalprefix, dp.internationalprefix, dp.unknownprefix, dp.subscriberprefix, dp.callednationalprefix, dp.calledinternationalprefix,
                      dp.calledunknownprefix, dp.calledsubscriberstripdigits, css7.name as cgpn_national_css, css8.name as cgpn_intl_css, css9.name as cgpn_unknown_css,
                      css10.name as cgpn_subscriber_css, css11.name as cdpn_national_css, css12.name as cdpn_intl_css, css13.name as cdpn_unknown_css, css14.name as cdpn_subscriber_css,
                      dp.nationalstripdigits, dp.internationalstripdigits, dp.unknownstripdigits, dp.subscriberstripdigits, dp.callednationalstripdigits, dp.calledinternationalstripdigits,
                      dp.calledunknownstripdigits, dp.calledsubscriberstripdigits, css15.name as Phone_cgpnCSS, css16.name as Phone_connectedCSS, css17.name as Phone_redirectCSS

                        from devicepool as dp left join callmanagergroup as cmg on dp.fkcallmanagergroup = cmg.pkid left join callingsearchspace as css1 on dp.fkcallingsearchspace_autoregistration = css1.pkid
                      left join callingsearchspace as css2 on dp.fkcallingsearchspace_adjunct=css2.pkid left join typerevertpriority as rp on dp.tkrevertpriority=rp.enum left join mraservicedomain as msd
                      on dp.fkmraservicedomain=msd.pkid left join datetimesetting as dt on dp.fkdatetimesetting=dt.pkid left join region as r on dp.fkregion=r.pkid left join mediaresourcelist as mrl on
                      dp.fkmediaresourcelist=mrl.pkid left join location as l on dp.fklocation=l.pkid left join typecountry as tkc on dp.tkcountry=tkc.enum left join srst as srst on dp.fksrst=srst.pkid left join
                      typebarge as tb on dp.tkbarge=tb.enum left join typestatus as tsjal on dp.tkstatus_joinacrosslines=tsjal.enum left join physicallocation as pl on dp.fkphysicallocation=pl.pkid
                      left join devicemobilitygroup as dmg on dp.fkdevicemobilitygroup=dmg.pkid left join wirelesslanprofilegroup as wlpg on dp.fkwirelesslanprofilegroup=wlpg.pkid
                      left join devicepoolroutegroupmap as dprg on dp.pkid=dprg.fkdevicepool left join routegroup as rg on dprg.fkroutegroup=rg.pkid left join
                      callingsearchspace as css3 on dp.fkcallingsearchspace_mobility=css3.pkid left join callingsearchspace as css4 on dp.fkcallingsearchspace_aar=css4.pkid
                      left join callingsearchspace as css5 on dp.fkcallingsearchspace_cgpntransform=css5.pkid left join callingsearchspace as css6 on dp.fkcallingsearchspace_cdpntransform=css6.pkid
                      left join geolocation as geo on dp.fkgeolocation = geo.pkid left join geolocationfilter as gf on dp.fkgeolocationfilter_lp = gf.pkid left join callingsearchspace as css7
                      on dp.fkcallingsearchspace_cgpnnational=css7.pkid left join callingsearchspace as css8 on dp.fkcallingsearchspace_cgpnintl=css8.pkid left join callingsearchspace as css9
                      on dp.fkcallingsearchspace_cgpnunknown=css9.pkid left join callingsearchspace as css10 on dp.fkcallingsearchspace_cgpnsubscriber=css10.pkid left join callingsearchspace as css11
                      on dp.fkcallingsearchspace_callednational=css11.pkid left join callingsearchspace as css12 on dp.fkcallingsearchspace_calledintl=css12.pkid left join callingsearchspace as css13
                      on dp.fkcallingsearchspace_calledunknown=css13.pkid left join callingsearchspace as css14 on dp.fkcallingsearchspace_calledsubscriber=css14.pkid left join callingsearchspace as css15
                      on dp.fkcallingsearchspace_cgpningressdn=css15.pkid left join callingsearchspace as css16 on dp.fkcallingsearchspace_cntdpntransform=css16.pkid left join callingsearchspace as css17 on
                      dp.fkcallingsearchspace_rdntransform= css17.pkid"""


geolocation_sql = """select name, country, description, a1 as State, a2 as County, a3 as City, a4 as Borough, a5 as Neighborhood, a6 as Street,
                    prd as Leading_Street, pod as Trailing_Street, sts as Avenue, hno as House_number, hns as House_number_suffix, lmk as Landmark, loc as Location, flr as floor,
                    nam as Resident,pc as zipcode from geolocation order by name"""


call_manager_group_sql = """select cmg.name as group, cm.name as cmgr, cmgm.priority from callmanagergroupmember as cmgm
            inner join callmanagergroup as cmg on cmgm.fkcallmanagergroup=cmg.pkid
            inner join callmanager as cm on cmgm.fkcallmanager=cm.pkid order by cmg.name
            """


css_sql = """select css.name as CSS_Name, css.description, rp.name as Route_Partition, cssm.sortorder from callingsearchspacemember as cssm
             inner join callingsearchspace as css  on cssm.fkcallingsearchspace=css.pkid
             left join routepartition as rp on cssm.fkroutepartition=rp.pkid order by css.name"""


partitions_sql = """select name, description from routepartition order by name"""


locations_sql = """select l1.name as location_a, l2.name as location_b, lm.weight, lm.kbits, lm.videokbits, lm.immersivekbits from locationmatrix as lm
            left join location as l1 on lm.fklocation_a=l1.pkid left join location as l2 on lm.fklocation_b=l2.pkid order by location_a"""


physical_locations_sql = (
    """select name, description from physicallocation order by name"""
)


sip_profiles_sql = """select sp.name, sp.description, sp.defaulttelephonyeventpayloadtype,tkg.name as Early_offer_for_gclear_calls,tus.name as
        User_Agent_and_server_Header_info,tcv.name as Version_in_UA_Server_Header,tud.name as Dial_String,tch.name as
        Confidential_access_level_headers,sp.zzredirectbyapp, sp.ringing180,sp.t38invite,sp.faxinvite,sp.enableurioutdialsupport,
        sp.isassuredsipserviceenabled,sp.enableexternalqos,tbm.name as sdp_bandwidth_modifier_for_earlyoffer_reinvites,sdp.name as sdp_transparency_profile,
        tsa.name as accept_audio_codec_pref_in_received_offer, sp.inactivesdprequired, sp.allowrrandrsbandwidthmodifier,
        sp.siptimerinviteexp, sp.siptimerregdelta, sp.siptimerregexpires, sp.siptimert1, sp.siptimert2,
        sp.sipretryinvite,sp.sipretrynoninvite, sp.sipstartmediaport, sp.zzstopmediaport, tdv.name as dscp_audio, tdv1.name as dscp_video,
        tdv2.name as dscp_for_audio_portion_of_video, tdv3.name as dscp_for_telepresence_calls, tdv4.name as audio_portion_of_telepresence,
        zzcallpickupuri as call_pickup_uri,zzcallpickuplisturi as call_pickup_group_other_uri, zzcallpickupgroupuri as call_pickup_group_uri,
        zzmeetmeserviceuri as meet_me_service_uri, tui.name as user_info,tdl.name as dtmf_db_level,tzp.name as call_hold_ring_back,
        tzp1.name as anoymous_call_block, tzp2.name as caller_ID_blocking, tzp3.name as DND_control, ttl.name as telnet_level,
        rpn.name as resource_priority_namespace_list, sp.zztimerkeepaliveexpires as timer_keep_alive_expires, sp.zztimersubscribeexpires
        as timer_subscribe_expires, sp.zztimersubscribedelta as timer_subscribe_delta, sp.zzmaxredirects as max_redirects, sp.zzoffhooktofirstdigittmr
        as off_hook_to_first_digit_tmr, sp.zzcallforwarduri as call_forward_uri, sp.zzcnfjoinenabled,sp.mlppuserauthorization, sp.isanonymous,
        sp.callername,sp.calleriddn,tsr.name as reroute_request_based_on,tsro.name as SIP_Rel1XX_Options, tvc.name as video_call_traffic_class,
        tcli.name as Calling_line_id_presentation, tsrm.name as
        session_refresh_method,sp.earlyofferforgclearenable,sp.enableanatforearlyoffercalls,sp.delivercnfbridgeid,sp.usecalleridcallernameinurioutgoingrequest,
        sp.rejectanonymousincomingcall, sp.rejectanonymousoutgoingcall,sp.destroutestring, sp.conncallbeforeplayingann, sp.enableoutboundoptionsping,
        sp.optionspingintervalwhenstatusok,sp.optionspingintervalwhenstatusnotok,sp.sipoptionspingtimer,sp.sipoptionspingretrycount,sp.sendrecvsdpinmidcallinvite,
        sp.allowpresentationsharingusingbfcp, sp.allowixchannel,sp.allowmultiplecodecsinanswersdp

        from sipprofile as sp left join typegclear as tkg on
        sp.tkgclear=tkg.enum left join typeuseragentserverheaderinfo as tus on sp.tkuseragentserverheaderinfo=tus.enum left join typecucmversioninsipheader as
        tcv on sp.tkcucmversioninsipheader=tcv.enum left join typeuridisambiguationpolicy as tud on sp.tkuridisambiguationpolicy=tud.enum left join typecalheaders
         as tch on sp.tkcalheaders=tch.enum left join typesipbandwidthmodifier as tbm on
        sp.tksipbandwidthmodifier=tbm.enum left join sdpattributelist as sdp on sp.fksdpattributelist=sdp.pkid
        left join typestatus as tsa on sp.tkstatus_handlingofreceivedoffercodecpreferences=tsa.enum left join typedscpvalue as tdv on
        sp.tkdscpvalue_audiocalls = tdv.enum left join typedscpvalue as tdv1 on sp.tkdscpvalue_videocalls=tdv1.enum left join typedscpvalue as tdv2 on
        sp.tkdscpvalue_audioportionofvideocalls = tdv2.enum left join typedscpvalue as tdv3 on sp.tkdscpvalue_telepresencecalls = tdv3.enum
        left join typedscpvalue as tdv4 on sp.tkdscpvalue_audioportionoftelepresencecalls = tdv4.enum left join typezzuserinfo as tui
        on sp.tkzzuserinfo=tui.enum left join typezzdtmfdblevel as tdl on sp.tkzzdtmfdblevel=tdl.enum left join typezzpreff as tzp
        on sp.tkzzpreff_zzcallholdringback=tzp.enum left join typezzpreff as tzp1 on sp.tkzzpreff_zzanonymouscallblock=tzp1.enum
        left join typezzpreff as tzp2 on sp.tkzzpreff_zzcalleridblocking=tzp2.enum left join typezzpreff as tzp3 on sp.tkzzpreff_zzdndcontrol
        = tzp3.enum left join typetelnetlevel as ttl on sp.tktelnetlevel=ttl.enum left join resourceprioritynamespacelist as rpn on
        sp.fkresourceprioritynamespacelist=rpn.pkid left join typesipreroute as tsr on sp.tksipreroute=tsr.enum left join typesiprel1xxoptions
        as tsro on sp.tksiprel1xxoptions=tsro.enum left join typevideocalltrafficclass as tvc on sp.tkvideocalltrafficclass=tvc.enum
        left join typecallinglineidentification as tcli on sp.tkcallinglineidentification = tcli.enum left join typesipsessionrefreshmethod
        as tsrm on sp.tksipsessionrefreshmethod=tsrm.enum where sp.isstandard = 'f' order by name"""


trunk_security_profile_sql = """select tsp.name,tsp.description,tds.name as devicesecuritymode, t.name as incomingtransporttype,to.name as
                                outgoingtransporttype,tsp.digestauthall, tsp.noncepolicytime,tsp.incomingport,tsp.applevelauth,
                                tsp.aclpresencesubscription, tsp.acloodrefer, tsp.aclunsolicitednotification, tsp.x509subjectname,
                                tsp.aclallowreplace,tsp.transmitsecuritystatus,tsp.allowchargingheader from securityprofile as tsp
                                left join typedevicesecuritymode as tds on tsp.tkdevicesecuritymode=tds.enum left join typetransport as t on
                                tsp.tktransport=t.enum left join typetransport as to on tsp.tktransport_out=to.enum where tksecuritypolicy=1 or
                                tksecuritypolicy=8 order by tsp.name"""


phone_security_profile_sql = """select psp.name, psp.description, psp.noncepolicytime, tds.name as devicesecuritymode, t.name as transporttype, psp.digestauthall,
                                psp.tftpencryptedflag, psp.sipoauthflag, tam.name as authenticationmode, tko.name as Key_Order, tks.name as RSA_Key_Size, tks1.name as EC_Key_Size
                                from securityprofile as psp left join typedevicesecuritymode as tds on psp.tkdevicesecuritymode=tds.enum
                                left join typetransport as t on psp.tktransport=t.enum left join typeauthenticationmode as tam on psp.tkauthenticationmode=tam.enum
                                left join typekeyorder as tko on psp.tkkeyorder=tko.enum left join typekeysize as tks on psp.tkkeysize=tks.enum left join typekeysize
                                as tks1 on psp.tkeckeysize=tks1.enum where (tksecuritypolicy=4 or tksecuritypolicy=99) and isstandard='f' order by psp.name"""


common_phone_profile_sql = """select cpc.name, cpc.description, tdnd.name as dnd_option, trs.name as dnd_incoming_call_alert, fcp.name as feature_control_policy, whsp.name as
                                wifi_hot_spot_profile,zzbackgroundimageaccess,tpp.name as phone_personalization,ts.name as always_use_prime_line,ts1.name as
                                always_use_prime_line_for_vm, tpsd.name as services_provisioning, vpng.name as vpn_group,vpnp.name as vpn_profile, cpcx.xml as xml
                                 from commonphoneconfig as cpc left join typedndoption as tdnd on cpc.tkdndoption=tdnd.enum
                                left join typeringsetting as trs on cpc.tkringsetting_dnd=trs.enum left join featurecontrolpolicy as fcp on cpc.fkfeaturecontrolpolicy=fcp.pkid
                                left join wifihotspotprofile as whsp on cpc.fkwifihotspotprofile=whsp.pkid left join typephonepersonalization as tpp on
                                cpc.tkphonepersonalization=tpp.enum left join typestatus as ts on cpc.tkstatus_alwaysuseprimeline=ts.enum left join
                                typestatus as ts1 on cpc.tkstatus_alwaysuseprimelineforvm=ts1.enum left join typephoneservicedisplay as tpsd on cpc.tkphoneservicedisplay
                                = tpsd.enum left join vpngroup as vpng on cpc.fkvpngroup=vpng.pkid left join vpnprofile as vpnp on cpc.fkvpnprofile=vpnp.pkid
                                left join commonphoneconfigxml as cpcx on cpcx.fkcommonphoneconfig=cpc.pkid order by name"""


region_matrix_sql = """select r1.name as regiona, r2.name as regionb, cl.name as codec, rm.audiobandwidth, rm.videobandwidth, rm.immersivebandwidth from regionmatrix as rm
        left join region as r1 on rm.fkregion_a=r1.pkid left join region as r2 on rm.fkregion_b=r2.pkid left join codeclist as cl on rm.fkcodeclist = cl.pkid
        order by r1.name"""


sip_trunks_sql = """select d.name, d.description, stcs.name as siptrunkcalllegsecurity, d.mtprequired, trp.name as usetrustedrelaypoint, d.retryvideocallasaudio, d.srtpallowed,
                    stsp.name as siptrunksecurityprofile, dsm.name as devicesecuritymode, sp.name as sipprofile, sp.zzredirectbyapp, sp.ringing180, sp.enableurioutdialsupport,
                    sdpattr.name as sdpattributelist, codecpref.name as handlingofreceivedoffercodecpreferences, sp.inactivesdprequired, sp.sendrecvsdpinmidcallinvite,
                    rel1xx.name as rel1xxoptions, srm.name as sipsessionrefreshmethod, eo.name as eosuppvoicecall, sp.enableoutboundoptionsping, sp.allowpresentationsharingusingbfcp,
                    sp.allowixchannel, sp.allowmultiplecodecsinanswersdp, dtmf.name as dtmfsignaling, d.pstnaccess, d.runonallnodes, qv.name qsigvariant, tp.name as tunneledprotocol,
                    snsprofile.name as sipnormalizationscript_profile, snsdevice.name as sipnormalizationscript_device

                    from sipdevice as sd inner join device as d on d.pkid=sd.fkdevice
                    inner join typedtmfsignaling as dtmf on dtmf.enum=d.tkdtmfsignaling inner join typestatus as trp on trp.enum=d.tkstatus_usetrustedrelaypoint
                    inner join typetunneledprotocol as tp on tp.enum=sd.tktunneledprotocol inner join typeqsigvariant as qv on qv.enum=sd.tkqsigvariant
                    inner join typesiptrunkcalllegsecurity as stcs on stcs.enum=sd.tksiptrunkcalllegsecurity inner join securityprofile as stsp on stsp.pkid=d.fksecurityprofile
                    inner join typedevicesecuritymode as dsm on dsm.enum=stsp.tkdevicesecuritymode inner join sipprofile as sp on sp.pkid=d.fksipprofile
                    inner join typestatus as codecpref on codecpref.enum=sp.tkstatus_handlingofreceivedoffercodecpreferences inner join typeeosuppvoicecall as eo on
                    eo.enum=sp.tkeosuppvoicecall inner join typesiprel1xxoptions as rel1xx on rel1xx.enum=sp.tksiprel1xxoptions inner join typesipsessionrefreshmethod as srm on
                    srm.enum=sp.tksipsessionrefreshmethod left outer join sipnormalizationscript as snsprofile on sp.fksipnormalizationscript=snsprofile.pkid left outer join
                    sipnormalizationscript as snsdevice on sd.fksipnormalizationscript=snsdevice.pkid left outer join sdpattributelist as sdpattr on sdpattr.pkid=sp.fksdpattributelist
                    order by d.name"""


mrgl_sql = """select mrl.name as MediaResourceList, mrg.name as MediaResourceGroup, mrgl.sortorder from mediaresourcelistmember as mrgl inner join mediaresourcelist as mrl on mrgl.fkmediaresourcelist=mrl.pkid
            inner join mediaresourcegroup as mrg on mrgl.fkmediaresourcegroup=mrg.pkid order by mrl.name"""


calling_party_transformation_pattern_sql = """select n.dnorpattern, rp.name as partition, n.description, n.patternurgency,n.mlpppreemptiondisabled,ts.name as UseCallingPartysExternalMask,
                                            ddi.name as discard_digits,n.callingpartytransformationmask, n.callingpartyprefixdigits, pb.name as calling_line_presentation,
                                            nt.name as calling_number_type, np.name as calling_numbering_plan from numplan as n left join routepartition as rp on n.fkroutepartition=rp.pkid
                                            left join typestatus as ts on n.tkstatus_usefullyqualcallingpartynum=ts.enum left join digitdiscardinstruction as ddi on
                                            n.fkdigitdiscardinstruction = ddi.pkid left join typepresentationbit as pb on n.tkpresentationbit_callingline = pb.enum
                                            left join typepriofnumber as nt on n.tkpriofnumber_calling = nt.enum left join typenumberingplan as np on
                                            n.tknumberingplan_calling = np.enum where tkpatternusage='15' order by n.dnorpattern"""


acpl_sql = """select cl.name as codeclist, tc.name as codec, clm.preferenceorder from codeclistmember as clm inner join codeclist as cl on clm.fkcodeclist=cl.pkid
              inner join typecodec as tc on clm.tkcodec=tc.enum where cl.isstandard='f' order by cl.name"""


ntp_server_sql = """select n.name as server, n.description, nm.name as mode from ntpserver as n inner join typezzntpmode as nm on n.tkzzntpmode=nm.enum order by n.name"""


date_time_group_sql = """select d.name, d.datetemplate, t.name as timezone, ns.name as ntp_server, nsd.selectionorder from datetimesetting as d
                        inner join typetimezone as t on d.tktimezone = t.enum left join ntpserverdatetimesettingmap as nsd on
                        nsd.fkdatetimesetting = d.pkid left join ntpserver as ns on nsd.fkntpserver = ns.pkid order by d.name, nsd.selectionorder"""


route_lists_sql = """select n.dnorpattern as pattern,d.name as RouteList,n.description, rg.name as route_group,rl.selectionorder from device as d left join devicenumplanmap as dnp on dnp.fkdevice=d.pkid
                    left join routelist as rl on rl.fkdevice=d.pkid right join numplan as n on dnp.fknumplan=n.pkid left join routegroup as
                    rg on rl.fkroutegroup=rg.pkid where n.tkpatternusage=5 or tkpatternusage=9"""


sip_route_patterns_sql = """select n.dnorpattern, n.description, rp.name as partition, n.blockenable, n.tkstatus_usefullyqualcallingpartynum as UseCallingPartysExternalMask,
                            n.callingpartytransformationmask, n.prefixdigitsout, pb.name as calling_line_presentation, pb1.name as calling_name_presentation,
                            pb2.name as connected_line_presentaion, pb3.name as connected_name_presentation from numplan as n inner join routepartition as rp on
                            n.fkroutepartition=rp.pkid left join typepresentationbit as pb on n.tkpresentationbit_callingline = pb.enum left join typepresentationbit as pb1 on
                            n.tkpresentationbit_callingname = pb1.enum left join typepresentationbit as pb2 on n.tkpresentationbit_connectedline = pb2.enum left join
                            typepresentationbit as pb3 on n.tkpresentationbit_connectedname = pb3.enum where tkpatternusage='9' order by n.dnorpattern"""


call_park_sql = """select n.dnorpattern, n.description, rp.name as partition, cm.name as CUCM from numplan as n inner join routepartition as rp on n.fkroutepartition=rp.pkid
                    left join callmanager as cm on n.fkcallmanager=cm.pkid where tkpatternusage='0' order by n.dnorpattern"""


softkey_template_sql = """select name, description from softkeytemplate order by name"""


uc_services_sql = """select uc.name, p.name as product, uc.description, uc.hostnameorip, uc.port, cp.name as protocol from ucservice as uc left join typeucproduct as p
                     on uc.tkucproduct=p.enum left join typeconnectprotocol as cp on uc.tkconnectprotocol=cp.enum order by uc.name"""


uc_service_profile_sql = """select ucsp.name,ucsp.description,tuc.name as type, uc.name as ucserviceprofile1,uc1.name as ucserviceprofile2,uc2.name as ucserviceprofile3,ucspdx.xml as xml
    from ucserviceprofile as ucsp left join ucserviceprofiledetail as ucspd on ucspd.fkucserviceprofile=ucsp.pkid left join typeucservice as tuc on ucspd.tkucservice=tuc.enum
    left join ucservice as uc on ucspd.fkucservice_1=uc.pkid left join ucservice as uc1 on ucspd.fkucservice_2=uc1.pkid left join ucservice as uc2 on ucspd.fkucservice_3=uc2.pkid
    left join ucserviceprofiledetailxml as ucspdx on ucspdx.fkucserviceprofiledetail=ucspd.pkid"""


feature_group_template_sql = """select fgt.name, fgt.description,fgt.islocaluser, fgt.cupsenabled, fgt.enablecalendarpresence,ucsp.name as ucserviceprofile,up.name as userprofile,
                                fgt.enableusertohostconferencenow,fgt.allowcticontrolflag, fgt.enableemcc, fgt.enablemobility,fgt.enablemobilevoice, fgt.maxdeskpickupwaittime,
                                fgt.remotedestinationlimit,pg.name as blf_presence_group,css.name as subscribe_css,ul.name as user_locale from featuregrouptemplate as fgt
                                left join ucserviceprofile as ucsp on fgt.fkucserviceprofile=ucsp.pkid left join ucuserprofile as up on fgt.fkucuserprofile=up.pkid
                                left join matrix as pg on fgt.fkmatrix_presence=pg.pkid left join callingsearchspace as css on fgt.fkcallingsearchspace_restrict = css.pkid
                                left join typeuserlocale as ul on fgt.tkuserlocale=ul.enum order by fgt.name"""


ldap_custom_filter_sql = """select name, filter from ldapfilter order by name"""

ldap_search_sql = """select lsa.enabledirectorysearch,lsa.distinguishedname, lsa.usersearchbase1, lsa.usersearchbase2, lsa.usersearchbase3, lf.name as filter,
                    lsa.enablerecursivesearch, ucsp.name as UC_service_primary, ucss.name as UC_service_secondary, ucst.name as UC_service_tertiary from ldapsearchagreement as lsa
                    left join ldapfilter as lf on lsa.fkldapfilter_user=lf.pkid left join ucservice as ucsp on lsa.fkucservice_primary=ucsp.pkid
                    left join ucservice as ucss on lsa.fkucservice_secondary=ucss.pkid left join ucservice as ucst on lsa.fkucservice_tertiary=ucst.pkid
                """


access_control_groups_sql = """select g.name as group, r.name as role from functionroledirgroupmap as rgmap inner join functionrole as r on rgmap.fkfunctionrole = r.pkid
            inner join dirgroup as g on rgmap.fkdirgroup=g.pkid where g.isstandard='f' order by g.name
            """


application_users_sql = """select au.name, dg.name as group, au.acloobsubscription, au.acloodrefer, au.aclpresencesubscription, au.aclunsolicitednotification,
                            au.aclallowreplace, au.userrank from applicationuser as au left join applicationuserdirgroupmap as audgmap on
                            audgmap.fkapplicationuser=au.pkid left join dirgroup as dg on audgmap.fkdirgroup=dg.pkid where au.isstandard='f'
                        """

remote_clusters_sql = """select clusterid, fullyqualifiedname, version from remotecluster order by clusterid"""


enterprise_parameters_sql = """select pc.paramname, pc.paramvalue from processconfig as pc where pc.tkservice=11 order by pc.paramname"""

expressway_c_sql = (
    """select hostnameorip, x509subjectnameoraltname from expresswaycconfiguration"""
)

external_call_control_profile_sql = """select eccp.name, eccp.primaryuri, eccp.secondaryuri, eccp.enableloadbalancing, eccp.routingrequesttimer,
                                        css.name as Diversion_CSS, ctof.name as calltreatment from externalcallcontrolprofile as eccp
                                        inner join typecalltreatmentonfailure as ctof on eccp.tkcalltreatmentonfailure=ctof.enum
                                        left join callingsearchspace as css on eccp.fkcallingsearchspace_diversionrerouting = css.pkid
                                    """


mra_service_domain_sql = (
    """select servicedomains, isdefault, name from mraservicedomain"""
)

phone_button_template_sql = """select name, numofbuttons, usermodifiable, privatetemplate from phonetemplate where usermodifiable='t'
                            """


templates = {
    "RoutePattern": route_pattern_sql,
    "TransPattern": translation_pattern_sql,
    "RouteGroup": route_groups_sql,
    "DevicePool": device_pools_sql,
    "GeoLocation": geolocation_sql,
    "CallManagerGroup": call_manager_group_sql,
    "Css": css_sql,
    "RoutePartition": partitions_sql,
    "Location": locations_sql,
    "PhysicalLocation": physical_locations_sql,
    "SipProfile": sip_profiles_sql,
    "SipTrunkSecurityProfile": trunk_security_profile_sql,
    "PhoneSecurityProfile": phone_security_profile_sql,
    "CommonPhoneConfig": common_phone_profile_sql,
    "RegionMatrix": region_matrix_sql,
    "SipTrunk": sip_trunks_sql,
    "MediaResourceList": mrgl_sql,
    "CallingPartyTransformationPattern": calling_party_transformation_pattern_sql,
    "AudioCodecPreferenceList": acpl_sql,
    "PhoneNtp": ntp_server_sql,
    "DateTimeGroup": date_time_group_sql,
    "RouteList": route_lists_sql,
    "SipRoutePattern": sip_route_patterns_sql,
    "CallPark": call_park_sql,
    "SoftKeyTemplate": softkey_template_sql,
    "UcService": uc_services_sql,
    "ServiceProfile": uc_service_profile_sql,
    "FeatureGroupTemplate": feature_group_template_sql,
    "LdapFilter": ldap_custom_filter_sql,
    "LdapSearch": ldap_search_sql,
    "UserGroup": access_control_groups_sql,
    "AppUser": application_users_sql,
    "RemoteCluster": remote_clusters_sql,
    "ServiceParameter": enterprise_parameters_sql,
    "ExpresswayCConfiguration": expressway_c_sql,
    "ExternalCallControlProfile": external_call_control_profile_sql,
    "MraServiceDomain": mra_service_domain_sql,
    "PhoneButtonTemplate": phone_button_template_sql,
}


CONFIG_FILE = Path.home() / ".cucmconfigtracker.json"


def load_or_prompt_config() -> dict:
    """Load config from file or prompt user and save."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        print(f"Loaded config from {CONFIG_FILE}")

        # Ask if user wants to reconfigure
        if inquirer.confirm(message="Use saved configuration?", default=True).execute():
            return config

    # Prompt for all values
    config = {
        "cucmpub": inquirer.text(
            message="Enter the CUCM node name",
        ).execute(),
        "cucm_axl_username": inquirer.text(
            message="Enter the CUCM username which has AXL API access",
        ).execute(),
        "cucm_axl_password": inquirer.secret(
            message="Enter the password for the above AXL username",
        ).execute(),
        "cucm_cli_username": inquirer.text(
            message="Enter the CUCM username which has admin CLI access",
        ).execute(),
        "cucm_cli_password": inquirer.secret(
            message="Enter the password for the above CLI username",
        ).execute(),
        "cucm_axl_api_wsdl_path": inquirer.text(
            message="Enter the location to find the CUCM AXL API WSDL path for the version of your CUCM",
        ).execute(),
        "config_relative_path": inquirer.text(
            message="Enter the location of the csv configuration files",
        ).execute(),
    }

    # Save config
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    CONFIG_FILE.chmod(0o600)  # Restrict permissions since it contains passwords
    print(f"Config saved to {CONFIG_FILE}")

    return config


def main() -> int:
    # Add this at the very beginning, before load_or_prompt_config()
    if "--reconfigure" in sys.argv:
        sys.argv.remove("--reconfigure")
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()

    certroot = os.getenv("REQUESTS_CA_BUNDLE", default="/etc/pki/tls/cert.pem")
    # Load or prompt for config
    config = load_or_prompt_config()
    cucmpub = config["cucmpub"]
    cucm_axl_username = config["cucm_axl_username"]
    cucm_axl_password = config["cucm_axl_password"]
    cucm_cli_username = config["cucm_cli_username"]
    cucm_cli_password = config["cucm_cli_password"]
    cucm_axl_api_wsdl_path = config["cucm_axl_api_wsdl_path"]
    config_relative_path = config["config_relative_path"]
    config_parent_parser = argparse.ArgumentParser(add_help=False)
    config_parent_parser.add_argument(
        "config_item",
        help='Config item to check. To get all valid config items, run "list_config" command',
    )
    email_recipient_parent_parser = argparse.ArgumentParser(add_help=False)
    email_recipient_parent_parser.add_argument(
        "--email",
        dest="email_recipient",
        default="uc-admin",
        help="Enter the email address to send the change summary details to",
    )
    commit_parent_parser = argparse.ArgumentParser(add_help=False)
    commit_parent_parser.add_argument(
        "commit",
        help="Specify the reason or the jira for the change",
    )

    parser = argparse.ArgumentParser(description="Choose a command to run.")
    subparser = parser.add_subparsers(dest="command")
    subparser.add_parser("list_all_configs", help="List all the available config items")
    subparser.add_parser(
        "check_running",
        parents=[
            config_parent_parser,
            email_recipient_parent_parser,
        ],
        help="Compares running config and base config of entered config item and notifies about changes, if any.",
    )
    subparser.add_parser(
        "update_base",
        parents=[
            config_parent_parser,
            email_recipient_parent_parser,
            commit_parent_parser,
        ],
        help="Update the base config with the most recent running config of the entered config item.",
    )
    subparser.add_parser(
        "check_all",
        parents=[email_recipient_parent_parser],
        help="Verifies all configs from the config items and notifies if there are any changes",
    )
    subparser.add_parser(
        "list_changes",
        help="List all the changes made in the database",
        parents=[email_recipient_parent_parser],
    )
    subparser.add_parser(
        "uconfigs_check",
        help="Command to run the monitoring check for all items, returns 0 if base and running configs are same, 1 if not",
    )

    args = parser.parse_args()

    valid_configitem = list(templates.keys())
    valid_configitem.sort()
    if args.command:
        if args.command == "list_all_configs":
            print("Valid config items are:\n\n{}".format("\n".join(valid_configitem)))
        elif args.command == "check_running":
            configitem = args.config_item
            if configitem not in valid_configitem:
                print(
                    'Config item " {} " is not a valid config. Valid config items are:\n\n{}'.format(
                        configitem, "\n".join(valid_configitem)
                    )
                )
                return 1
            else:
                service, history = create_service(
                    cucmpub=cucmpub,
                    username=cucm_axl_username,
                    password=cucm_axl_password,
                    certroot=certroot,
                    wsdl_path=cucm_axl_api_wsdl_path,
                )
                sql = templates[configitem]
                resp = execute_sql_query(service, history, sql)
                try:
                    update_runningconfig(
                        cucmpub,
                        config_relative_path,
                        configitem,
                        resp,
                        email_recipient=args.email_recipient,
                    )
                except Exception as e:
                    print(e)
                    return 1
        elif args.command == "update_base":
            configitem = args.config_item
            if configitem not in valid_configitem:
                print(
                    'Config item " {} " is not a valid config. Valid config items are:\n\n{}'.format(
                        configitem, "\n".join(valid_configitem)
                    )
                )
                return 1
            else:
                update_baseconfig(
                    cucmpub,
                    config_relative_path,
                    configitem,
                    getpass.getuser(),
                    args.commit,
                    email_recipient=args.email_recipient,
                )
        elif args.command == "check_all":
            service, history = create_service(
                cucmpub=cucmpub,
                username=cucm_axl_username,
                password=cucm_axl_password,
                certroot=certroot,
                wsdl_path=cucm_axl_api_wsdl_path,
            )
            auto_check(
                cucmpub,
                config_relative_path,
                service,
                history,
                email_recipient=args.email_recipient,
            )
        elif args.command == "list_changes":
            service, history = create_service(
                cucmpub=cucmpub,
                username=cucm_axl_username,
                password=cucm_axl_password,
                certroot=certroot,
                wsdl_path=cucm_axl_api_wsdl_path,
            )
            exit_code = list_change(
                cucmpub,
                config_relative_path,
                cucm_cli_username,
                cucm_cli_password,
                service,
                history,
                templates,
                email_recipient=args.email_recipient,
            )
            return exit_code
        elif args.command == "uconfigs_check":
            exit_code = ucconfig_diff_check(config_relative_path, valid_configitem)
            return exit_code
        else:
            parser.print_help()
            return 1
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
