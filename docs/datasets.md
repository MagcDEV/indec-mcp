# INDEC API datasets

Generated from `spec/indec-openapi.json` by `scripts/gen_datasets_md.py`. Do not edit by hand.

Base URL: `https://api.indec.gob.ar/v1/`. 19 endpoints, all `GET`, all require a Bearer JWT.

## Dirección de Estadísticas Mineras, Manufactureras, Energéticas y de la Construcción

### `/ipi_manufacturero` — Índice de producción industrial manufacturero

Índice de producción industrial manufacturero (IPI manufacturero), nivel general y aperturas por actividad económica, base 2004=100. Incluye las series original, desestacionalizada y tendencia-ciclo, expresadas como números índice y variaciones porcentuales.

- **Primary key:** `periodo`, `indicador`, `cuadro_web`, `codigo_CLaNAE_2004`, `descripcion`, `serie`, `tipo_de_indicador`
- **Columns:** `periodo`, `anio`, `mes`, `indicador`, `cuadro_web`, `codigo_CLaNAE_2004`, `descripcion`, `serie`, `tipo_de_indicador`, `unidad_de_medida`, `valor`

### `/ipi_minero` — Índice de producción industrial minero

IPI minero nivel general. Series original, desestacionalizada y tendencia-ciclo, base 2016=100. Este endpoint contiene información desde enero de 2017. Para más información, se puede consultar la metodología INDEC N° 41, Índice de producción industrial minero, a través del siguiente enlace: https://www.indec.gob.ar/ftp/cuadros/economia/metodologia_ipi_minero.pdf

- **Primary key:** `periodo`, `anio`, `mes`, `indicador`, `cuadro_web`, `codigo_ClaNAE_2010`, `codigo_CPC_2_1`, `serie`, `descripcion`, `tipo_de_indicador`, `unidad_de_medida`
- **Columns:** `periodo`, `anio`, `mes`, `indicador`, `cuadro_web`, `codigo_ClaNAE_2010`, `codigo_CPC_2_1`, `serie`, `descripcion`, `tipo_de_indicador`, `unidad_de_medida`, `valor`

## Dirección de Estadísticas Poblacionales

### `/estimaciones_poblacion_departamento_2022_2035` — Estimaciones de población por departamento y sexo

Estimaciones de población al 1° de julio de cada año calendario, por sexo y departamento, con base en el Censo Nacional de Población, Hogares y Viviendas 2022. Años 2022-2035.

- **Primary key:** `departamento_cod`, `sexo_cod`, `fecha`
- **Columns:** `jurisdiccion_cod`, `jurisdiccion_nom`, `departamento_cod`, `departamento_nom`, `rmba`, `sexo_cod`, `sexo_nom`, `poblacion`, `fecha`

### `/proyecciones_poblacion_jurisdiccion_2022_2040` — Proyecciones de población por jurisdicción, sexo y edad

Estimaciones y proyecciones de población al 1° de julio de cada año calendario, por sexo y grupos de edad, con base en el Censo Nacional de Población, Hogares y Viviendas 2022. Jurisdicciones. Años 2022-2040.

- **Primary key:** `jurisdiccion_cod`, `sexo_cod`, `edad_cod`, `fecha`
- **Columns:** `jurisdiccion_cod`, `jurisdiccion_nom`, `sexo_cod`, `sexo_nom`, `edad_cod`, `edad_nom`, `poblacion`, `fecha`

### `/proyecciones_poblacion_nacional_2022_2040` — Proyecciones nacionales de población por sexo y edad

Estimaciones y proyecciones de población al 1° de julio de cada año calendario, por sexo y edad, con base en el Censo Nacional de Población, Hogares y Viviendas 2022. Total del país. Años 2022-2040.

- **Primary key:** `edad`, `sexo_cod`, `fecha`
- **Columns:** `edad`, `sexo_cod`, `sexo_nom`, `poblacion`, `fecha`

## Dirección de Índices de Precios de Consumo

### `/ipc_nacional` — Índice de Precios al Consumidor (IPC)

Índice de precios al consumidor (IPC) de la República Argentina, con aperturas por región, división, grupo, clase y subclase, desde diciembre de 2016.

- **Primary key:** `clasificador`, `nivel`, `codigo`, `descripcion`, `periodo`, `anio`, `mes`, `indicador`, `tipo_de_indicador`, `tipo_de_indicador_descripcion`, `region`, `unidad_de_medida`
- **Columns:** `clasificador`, `nivel`, `codigo`, `descripcion`, `periodo`, `anio`, `mes`, `indicador`, `tipo_de_indicador`, `tipo_de_indicador_descripcion`, `region`, `unidad_de_medida`, `valor`, `observaciones`

### `/valorizacion_canastas_cba_cbt` — Valorización de las canastas básicas (CBA y CBT)

Valorización mensual de la canasta básica alimentaria (CBA) y de la canasta básica total (CBT) desde abril de 2016. Gran Buenos Aires.

- **Primary key:** `clasificador`, `nivel`, `codigo`, `descripcion`, `periodo`, `anio`, `mes`, `indicador`, `tipo_de_indicador`, `tipo_de_indicador_descripcion`, `region`, `unidad_de_medida`
- **Columns:** `clasificador`, `nivel`, `codigo`, `descripcion`, `periodo`, `anio`, `mes`, `indicador`, `tipo_de_indicador`, `tipo_de_indicador_descripcion`, `region`, `unidad_de_medida`, `valor`, `observaciones`

## Dirección de Índices de Precios de la Producción

### `/indice_salarios` — Índice de salarios

El índice de salarios estima la evolución de los salarios pagados en la economía aislando el indicador de variaciones relacionadas con conceptos tales como la cantidad de horas trabajadas, descuentos por ausentismo, premios por productividad y todo otro concepto asociado al desempeño o a las características de los individuos concretos.

- **Primary key:** `clasificador`, `nivel`, `codigo`, `descripcion`, `periodo`, `anio`, `mes`, `indicador`, `tipo_de_indicador`, `tipo_de_indicador_descripcion`, `unidad_de_medida`
- **Columns:** `clasificador`, `nivel`, `codigo`, `descripcion`, `periodo`, `anio`, `mes`, `indicador`, `tipo_de_indicador`, `tipo_de_indicador_descripcion`, `unidad_de_medida`, `valor`, `observaciones`

### `/icc_precios_construccion` — Índice del costo de la construcción

Índice del costo de la construcción (ICC) en el Gran Buenos Aires, base 1993.

- **Primary key:** `indicador`, `codigo_apertura`, `codigo_CPC_1`, `clasificador`, `nivel`, `descripcion`, `periodo`, `anio`, `mes`, `tipo_de_indicador`, `tipo_de_indicador_descripcion`, `region`, `unidad_de_medida`
- **Columns:** `indicador`, `codigo_apertura`, `codigo_CPC_1`, `clasificador`, `nivel`, `descripcion`, `periodo`, `anio`, `mes`, `tipo_de_indicador`, `tipo_de_indicador_descripcion`, `region`, `unidad_de_medida`, `valor`, `observaciones`

### `/redeterminacion_precios_contratos_obras_publicas` — Redeterminación de precios de obras públicas

Aperturas del índice del costo de la construcción (ICC) y del índice de precios internos básicos al por mayor (IPIB) utilizadas para la redeterminación periódica de precios de contratos de obra pública, según el Decreto 1295/2002.

- **Primary key:** `indicador`, `operativo`, `inciso`, `descripcion_inciso`, `codigo_apertura`, `clasificador`, `nivel`, `descripcion`, `origen`, `periodo`, `anio`, `mes`, `tipo_de_indicador`, `tipo_de_indicador_descripcion`, `region`, `unidad_de_medida`
- **Columns:** `indicador`, `operativo`, `inciso`, `descripcion_inciso`, `codigo_apertura`, `codigo_CPC_1`, `codigo_CIUU_3`, `clasificador`, `nivel`, `descripcion`, `origen`, `periodo`, `anio`, `mes`, `tipo_de_indicador`, `tipo_de_indicador_descripcion`, `region`, `unidad_de_medida`, `valor`, `observaciones`

### `/sipm_precios_mayoristas` — Sistema de índices de precios mayoristas

El Sistema de índices de precios mayoristas (SIPM) está compuesto por el índice de precios internos al por mayor (IPIM), el índice de precios internos básicos al por mayor (IPIB) y el índice de precios básicos del productor (IPP). Este endpoint contiene información desde diciembre de 2015.

- **Primary key:** `operativo`, `indicador`, `clasificador`, `nivel`, `codigo_apertura`, `descripcion`, `periodo`, `anio`, `mes`, `tipo_de_indicador`, `tipo_de_indicador_descripcion`, `region`, `unidad_de_medida`
- **Columns:** `operativo`, `indicador`, `clasificador`, `nivel`, `codigo_apertura`, `descripcion`, `periodo`, `anio`, `mes`, `tipo_de_indicador`, `tipo_de_indicador_descripcion`, `region`, `unidad_de_medida`, `valor`, `observaciones`

## Dirección Nacional de Cuentas Nacionales

### `/estimador-mensual-de-actividad-economica-nivel-general` — Estimador mensual de actividad económica. Nivel general

Estimador mensual de actividad económica (EMAE), nivel general, base 2004=100. Incluye la serie original, desestacionalizada y tendencia-ciclo, expresadas como números índice y variaciones porcentuales.

- **Primary key:** `tipo_de_indicador_descripcion`, `anio`, `mes`, `periodo`, `tipo_de_indicador`
- **Columns:** `tipo_de_indicador_descripcion`, `anio`, `mes`, `periodo`, `tipo_de_indicador`, `valor`, `observaciones`

### `/estimador-mensual-de-actividad-economica-sectores` — Estimador mensual de actividad económica. Sectores

Estimador mensual de actividad económica (EMAE) por sector de actividad económica, base 2004=100 (serie original). Incluye números índice y variaciones porcentuales interanuales para cada sector desde enero de 2004.

- **Primary key:** `anio`, `mes`, `codigo`, `descripcion`, `periodo`, `tipo_de_indicador`
- **Columns:** `anio`, `mes`, `codigo`, `descripcion`, `periodo`, `tipo_de_indicador`, `valor`, `observaciones`

## Dirección Nacional de Estadísticas de Condiciones de Vida

### `/pobreza_eph` — Incidencia de la pobreza y la indigencia (EPH)

Incidencia de la pobreza y la indigencia en hogares y personas a partir de la Encuesta Permanente de Hogares (EPH) para el total de 31 aglomerados urbanos. La información se presenta con frecuencia semestral desde el segundo semestre de 2016.

- **Primary key:** `area_geografica_nom`, `periodo`
- **Columns:** `area_geografica_nom`, `area_geografica_cod`, `periodo`, `anio`, `semestre`, `tasa_pobreza_hogares`, `tasa_pobreza_personas`, `tasa_indigencia_hogares`, `tasa_indigencia_personas`, `observaciones`

### `/mercado_trabajo_eph` — Principales tasas del mercado de trabajo (EPH)

Principales tasas del mercado de trabajo estimadas a partir de la Encuesta Permanente de Hogares (EPH), correspondientes al total de 31 aglomerados urbanos. Incluye las tasas de actividad, empleo, empleo formal e informal, desocupación, ocupación demandante de empleo y subocupación. La información se presenta con frecuencia trimestral desde el segundo trimestre de 2016.

- **Primary key:** `area_geografica_nom`, `periodo`
- **Columns:** `area_geografica_nom`, `area_geografica_cod`, `periodo`, `anio`, `trimestre`, `tasa_de_actividad`, `tasa_de_empleo`, `tasa_de_empleo_formal`, `tasa_de_empleo_informal`, `tasa_de_desocupacion`, `tasa_de_ocupacion_demandante`, `tasa_de_subocupacion`, `tasa_de_subocupacion_demandante`, `tasa_de_subocupacion_no_demandante`, `observaciones`

### `/canasta-crianza` — Valorización de la canasta de crianza

Valorización mensual de la canasta de crianza de la primera infancia, la niñez y la adolescencia. Está conformada por el costo de los bienes y servicios, y la valorización del tiempo de cuidado. La estimación se realiza para cuatro tramos de edad: menores de 1 año, de 1 a 3 años, de 4 a 5 años y de 6 a 12 años.

- **Primary key:** `Periodo`
- **Columns:** `anio`, `Mes`, `Periodo`, `bys_men1`, `cuid_men1`, `tot_men1`, `bys_1a3`, `cuid_1a3`, `tot_1a3`, `bys_4a5`, `cuid_4a5`, `tot_4a5`, `bys_6a12`, `cuid_6a12`, `tot_6a12`

## Dirección Nacional de Estadísticas del Sector Externo y Cuentas Internacionales

### `/comercio_exterior_nomenclaturas` — Códigos y denominaciones de comercio exterior

Diccionario de códigos y nomenclaturas utilizados en los servicios de datos de comercio exterior de la Dirección Nacional de Estadísticas del Sector Externo y Cuentas Internacionales.

- **Primary key:** `nombre_columna`, `codigo`
- **Columns:** `nombre_columna`, `codigo`, `codigo_descripcion`

### `/comercio_exterior_exportacion_bienes` — Exportaciones de bienes por rubros y países de destino

Exportaciones argentinas de bienes por grandes rubros, subrubros y país de destino desde enero de 1986. Incluye el valor FOB expresado en dólares estadounidenses y el peso neto de las mercaderías expresado en kilogramos.

- **Primary key:** `Anio`, `Mes`, `Periodo`, `rubro_cod`, `subrubro_cod`, `pais_cod`
- **Columns:** `Anio`, `Mes`, `Periodo`, `rubro_cod`, `subrubro_cod`, `pais_cod`, `valor_FOB`, `Peso_Neto`

### `/comercio_exterior_importacion_bienes` — Importaciones de bienes por uso económico y país de origen

Importaciones argentinas de bienes por uso económico, grandes categorías económica y país de origen. Incluye el valor CIF expresado en dólares estadounidenses y el peso neto de las mercaderías expresado en kilogramos. La información se presenta con frecuencia mensual.

- **Primary key:** `Anio`, `Mes`, `Periodo`, `uso_economico_cod`, `gce_cod`, `pais_cod`
- **Columns:** `Anio`, `Mes`, `Periodo`, `uso_economico_cod`, `gce_cod`, `pais_cod`, `valor_CIF`, `peso_neto`

