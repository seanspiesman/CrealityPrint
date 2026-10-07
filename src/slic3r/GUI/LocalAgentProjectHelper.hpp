#ifndef slic3r_GUI_LocalAgentProjectHelper_hpp_
#define slic3r_GUI_LocalAgentProjectHelper_hpp_

namespace Slic3r { namespace GUI {
class GUI_App;

// Runs one isolated local-agent operation after the private Plater is ready.
void run_local_agent_project_helper(GUI_App &app);

// Reads explicit preset JSON through the native config schema without entering wx.
// This entrypoint is dispatched directly by GUI_Run before wxEntry.
int run_local_agent_normalize_cli(int argc, char **argv);
} }

#endif
