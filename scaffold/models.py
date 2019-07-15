import numpy as np

class CellType:

    def __init__(self, name, density = 0.):
        self.name = name
        self.density = density
        self.color = '#000000'

class GeometricCellType(CellType):
    pass

class MorphologicCellType(CellType):
    pass

class Layer:

    def __init__(self, name, origin, dimensions):
        # Name of the layer
        self.name = name
        # The XYZ coordinates of the point at the center of the bottom plane of the layer.
        self.origin = origin
        # Dimensions in the XYZ axes.
        self.dimensions = dimensions

    @property
    def volume(self):
        return np.prod(self.dimensions)
