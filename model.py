class AppModel:
    def __init__(self):
        self.connections = {"Microfluidics": False, "Dobot": False, "Microcontroller": False}
        self.lipid_configs = ["Lipid A", "Lipid B", "Lipid C"]
        self.experiments = []
        self.compositions = {}
        self.buffers = {}
        self.status = "Idle"

    def toggle_connection(self, name):
        self.connections[name] = not self.connections[name]
        return self.connections[name]

    def add_lipid_config(self, name, concentration, mw):
        self.lipid_configs.append({"name": name, "concentration": concentration, "mw": mw})

    def add_experiment(self, exp_data):
        self.experiments.append(exp_data)

    def set_status(self, status):
        self.status = status
