#ifndef slic3r_GUI_LocalAgentProjectHelper_hpp_
#define slic3r_GUI_LocalAgentProjectHelper_hpp_

namespace Slic3r { namespace GUI {
class GUI_App;

// Runs one isolated local-agent operation after the private Plater is ready.
void run_local_agent_project_helper(GUI_App &app);
} }

#endif
