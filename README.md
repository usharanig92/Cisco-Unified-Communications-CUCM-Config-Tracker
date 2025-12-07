# Cisco-Unified-Communications-CUCM-Configuration-Tracker
Scripts for managing Cisco Unified Communications Manager

## Use Case
This script is designed for continuous monitoring of configuration changes in Cisco Unified Communications Manager and emails admin team with the change details, if the change is detected and it is different from the standard base config that was configured during the initial installation. The admin can then review the change and commit the new config to the base config, if it was intentional or revert if it was for testing or accidental change.

### Code Base Logic
The script uses the CUCM AXL List Change API to monitor for any changes in the database. List change API provides the following details if there are any changes in the system.

  **action** - indicates the type change: u is update, a is add, r is remove  
  **doGet** - Boolean value indicates when the client should perform a get operation to get the full details of the object.  
  **type** - Changed configuration item. Ex: DevicePool, RoutePattern, TransPattern.  
  **ChangedTags** - Contains name of the configuration field that was changed and the changed value. For example, Changed Configuration field is Description and the value is "Jon Doe".  
  Based on the action keyword, it can be determined whether this was the new add or update or remove.

Here is the sample output, which indicates that the new routepattern was added, routelist was updated, devicepool name was changed and provides the old value and new value. UUID field indicates the unique identifier of the each configuration item. This UUID field is being used to retrieve the new config from CUCM and update the running config file.

  <img width="1386" height="614" alt="image" src="https://github.com/user-attachments/assets/3835bc35-93a6-4ab2-ba25-72cf533a894e" />

Upon receiving the change details, based on the type, action and the change details, the script pulls the complete configuration details from CUCM using sql query and updates the corresponding running configuration file and emails the admin team notifiying the changed item and the procedure to commit the change to the base config.

## Installation

Clone the repo

`git
git clone https://github.com/usharanig92/Cisco-Unified-Communications-CUCM-Config-Tracker.git
`  

This script uses uv to install the required depenedencies. The script can be simply run with the command `uv run cucmconfigtracker.py <necessary-arguments>`. Refer https://github.com/astral-sh/uv to know more about uv package manager.

BaseConfigFile - Create the directory called baseconfig and copy the csv templates present under the template folder to store the base config. 
RunningConfigFile - Create the directory called runningconfig in the same location as baseconfig. Nothing else needed.

This location of the BaseConfigFile and RunningConfigFile is required in the script to constantly update the running config and the base config.  In the script the below function refers to the location of the config paths. Please update this function with the location of the above files.

```Python

def get_config_relative_path(which_config, name, mode):
    path = f"/usr/usha/application/unified-communications/{mode}/configs" # update the path to be the location of your BaseConfigFile and RunningConfigFile.
    config_path = os.path.join(path, which_config, name) + ".csv"
    return config_path

```

## Usage

The csv's that are copied into the baseconfig directory are empty files. When the script first runs, it copies the header from the baseconfig template csv's and create a running config csv in the runningconfig directory. Then the sql query is been made to the CUCM to pull all the config and updates the runningconfig csv's.

After the first run, all the configurations from CUCM has been pulled and stored as a csv in the running config. Now, the templates in the baseconfig directory can be replaced with the runningconfig csv's by directly copying over the files (only during the initial setup and then monitor and commit going forward)

Please note that in the script, the configuration items that are to be monitored are mentioned under templates as a key value pair. Key indicates the name of the configuration item such as DevicePool, TransPattern, RoutePattern and value being the sql query to pull the details of those items. The format of key in the template variable is same as what the listChange API outputs when the particular configuration has been changed.If you would like to add additional items to monitor, then use the exact syntax mentioned in the AXL Schema Reference guide per your CUCM verison - https://developer.cisco.com/docs/axl-schema-reference/.

<img width="1538" height="370" alt="image" src="https://github.com/user-attachments/assets/b5600808-f0fd-4f6a-baa0-e8213aa6a063" />

list_all_configs lists all the configuration items that this script currently monitors.

<img width="640" height="940" alt="image" src="https://github.com/user-attachments/assets/2d7550fe-7a04-48f7-9aa9-7ecad06e836e" />

Initialize the script by selecting the command list_changes with the mode parameter to initiate the listChange API request and for continuous monitoring. This app should run continuously to receive changes from CUCM and update the corresponding running config file and to accept the commit messages from admin. After admin commits the change with the message, a confirmation email will be sent to the admin team with the name of the committer, commit message and the change details.

<img width="1390" height="476" alt="image" src="https://github.com/user-attachments/assets/943cbed1-74f4-4d25-9eb9-ebac5b314f63" />


