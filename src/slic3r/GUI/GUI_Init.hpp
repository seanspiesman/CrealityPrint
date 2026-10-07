#ifndef slic3r_GUI_Init_hpp_
#define slic3r_GUI_Init_hpp_

#include <libslic3r/Preset.hpp>
#include <libslic3r/PrintConfig.hpp>

namespace Slic3r {

namespace GUI {

struct GUI_InitParams
{
	int		                    argc;
	char	                  **argv;

	// Substitutions of unknown configuration values done during loading of user presets.
	PresetsConfigSubstitutions  preset_substitutions;

    std::vector<std::string>    load_configs;
    DynamicPrintConfig          extra_config;
    std::vector<std::string>    input_files;

    // Private, one-shot local-agent helper mode. These fields are populated only
    // by GUI_Run's exact command-line switch parser, before single-instance IPC.
    bool                        local_agent_helper { false };
    std::string                 local_agent_action;
    std::string                 local_agent_argument;

    //BBS: remove start_as_gcodeviewer logic
	//bool	                    start_as_gcodeviewer;
	bool                        input_gcode { false };
};

int GUI_Run(GUI_InitParams &params);
int GUI_Run(int argc, char **argv);

} // namespace GUI
} // namespace Slic3r

#endif // slic3r_GUI_Init_hpp_
