#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Filename: LEAF.py
Author: Richard Fernandes
Date: 2025-09-15
Version: 1.0
Description: 
    Landscape Evolution and Forecasting Toolbox - LEAF

    LEAF maps vegetation biophysical variables using optical satellite imagery archived
    in Google Earth Engine (GEE) (https://earthengine.google.com/).  

    Results can be either in the format of time series of samples, output in CSV format, 
    or raster images, in GEOTIFF format, within regions defined by GEE vector feature collections.
    The time series can be specified using seasonal windows or using a date attached to 
    each GEE feature.    

    Limitations:
    Maximum of 5000 input satellite images for each mapped region.
    Circa 2020 land cover map is used to mask areas not considered land and for SL2P algorithm.
    using LEAF.sampleSites with 'local' option for output can crash for many sites due to GEE time out
    using LEAF.sampleSites with 'drive' option will send output to the root drive folder as 
        GEE will otherwise create a new folder for each sampled site due to a GEE bug

License: MIT License, the user must also respect the GEE terms and conditions
Contact: richard.fernandes@canada.ca
Dependencies: os, sys
"""

# import modules
import pandas as pd
import ee
import geetools #noqa: F401
import toolsUtils
import eoImage
import toolsNets
import eoImage
import toolsUtils
import dictionariesSL2P 
from datetime import timedelta
from datetime import datetime
import pickle
import os
from pprint import pprint
import numpy as np
from tqdm import tqdm 


#makes products for specified region and time period using selected version of SL2P processor
def makeProductCollection(colOptions,
                          netOptions,
                          variable,
                          mapBounds,
                          startDate,
                          endDate,
                          rangeStart,
                          rangeEnd,
                          rangeField,
                          maxCloudcover,
                          inputScale,
                          outputScale) :
 

    #identify tools for image processing based on input collection
    tools = colOptions['tools']
    
    # check how many different unique networks are available (i.e. by partition class) - this is used for SL2P-CCRS
    numNets = ee.Number(ee.Feature((colOptions["Network_Ind"]).first()).propertyNames().remove('lon').remove('Feature Index').remove('system:index').size())

    # populate the networks for each unique partition class
    SL2P = ee.List.sequence(1,ee.Number(colOptions["numVariables"]),1).map(lambda netNum: toolsNets.makeNetVars(colOptions["Collection_SL2P"],numNets,netNum))
    SL2Puncertainty = ee.List.sequence(1,ee.Number(colOptions["numVariables"]),1).map(lambda netNum: toolsNets.makeNetVars(colOptions["Collection_SL2Perrors"],numNets,netNum))

    # make products 
    input_collection =  ee.ImageCollection(colOptions['name']) \
                      .filterBounds(mapBounds) \
                      .filterDate(startDate, endDate) \
                      .filterMetadata(colOptions["Cloudcover"],'less_than',maxCloudcover) \
                      .filter(ee.Filter.calendarRange(rangeStart,rangeEnd,rangeField)) \
                      .limit(5000) \
                      .map(lambda image: image.clip(mapBounds)) \
                      .map(lambda image: tools.MaskClear(image))  \
                      .map(lambda image: eoImage.attach_Date(image)) \
                      .map(lambda image: eoImage.attach_LonLat(image)) \
                      .map(lambda image: tools.addGeometry(colOptions,image)) 


    # check if there are products
    if (input_collection.size().getInfo() > 0):

        # reproject to output scale based if it differs from nominal scale of first band
        projection = input_collection.first().select(netOptions["inputBands"][3]).projection()
        input_collection = input_collection.map( lambda image: image.setDefaultProjection(crs=image.select(image.bandNames().slice(0,1)).projection()) \
                                                                    .reduceResolution(reducer= ee.Reducer.mean(),maxPixels=1024).reproject(crs=projection,scale=inputScale))
        
        # Mask areas not mapped as land using ancillary land cover map
        input_collection  =  input_collection.map(lambda image: tools.MaskLand(image)).map(lambda image:toolsUtils.scaleBands(netOptions["inputBands"],netOptions["inputScaling"],netOptions["inputOffset"],image)) 

        # Add band using ancillary land cover map
        partition = (colOptions["partition"]).filterBounds(mapBounds).mosaic().clip(mapBounds).rename('partition');
        input_collection  =  input_collection.map(lambda image: image.addBands(partition))
        
        #  add s2cloudness data (For Sentinel-2)
        if colOptions['name'].startswith('COPERNICUS/S2_SR'):
            s2_cloudless_col = ee.ImageCollection('COPERNICUS/S2_CLOUD_PROBABILITY').filterBounds(mapBounds).filterDate(startDate, endDate) #.filter(ee.Filter.calendarRange(start,end,field))
            s2_cloudless_col = s2_cloudless_col.map( lambda image: image.setDefaultProjection(crs=image.select(image.bandNames().slice(0,1)).projection()) \
                                                                        .reduceResolution(reducer= ee.Reducer.mean(),maxPixels=1024).reproject(crs=projection,scale=inputScale)
                                                                        .rename('s2cloudless_probability'))
            input_collection = input_collection.combine(s2_cloudless_col)
              
        # determine if we need to apply networks or just resampled input data and partition for networks (in case they are applied to input after)
        if variable == "Surface_Reflectance":
            outputs = input_collection
        else:
            # pre process input imagery and flag invalid inputs
            input_collection  =  input_collection.map(lambda image: toolsUtils.invalidInput(colOptions["sl2pDomain"],netOptions["inputBands"],image)) 
            
            # extract essential input bands to keep with output
            if 's2cloudless_probability' in input_collection.first().bandNames().getInfo():
                outputs =  input_collection.select(['date','QC','longitude','latitude','s2cloudless_probability'])  
            else:
                outputs =  input_collection.select(['date','QC','longitude','latitude']) 
                
            # apply regression predictors (neural networks) to produce outputs
            estimateSL2P = input_collection.map(lambda image: toolsNets.wrapperNNets(SL2P,partition, netOptions, colOptions,"estimate",variable,image))
            uncertaintySL2P = input_collection.map(lambda image: toolsNets.wrapperNNets(SL2Puncertainty,partition, netOptions, colOptions,"error",variable,image))
            outputs =  outputs.combine(estimateSL2P).combine(uncertaintySL2P.select("error"+variable)) \
                            .map( lambda image: image.setDefaultProjection(crs=image.select(image.bandNames().slice(0,1)).projection()) \
                                                                        .reduceResolution(reducer= ee.Reducer.mean(),maxPixels=1024) \
                                                                        .reproject(crs=projection,scale=outputScale))
  
    else:
        # return an empty image collection if no input images were found
        outputs = ee.ImageCollection([]);
        
    return outputs

# returns lists of sampled values for each band in an image as a new feature property
def sampleProductCollection(productCollection, sampleRegion, outputScale, factor=1,numPixels=0) :

    # cast input and output variables as GEE objects
    productCollection = ee.ImageCollection(productCollection)
    outputScale= ee.Number(outputScale)
    sampleRegion = ee.Feature(sampleRegion)

    # produce feature collection where each feature corresponds to a list of samples from a given band from one product image
    #we use the 5th band to define the projection as the first 4 may have ancillary layers we added in different projections
    if (numPixels>0):
        sampleData = productCollection.map(lambda image: image.sample(region=sampleRegion.geometry(),\
                            projection=image.select(image.slice(4,5).bandNames()).projection(), scale=outputScale,geometries=False, dropNulls = True, numPixels=numPixels) ).flatten()
    else:
        sampleData = productCollection.map(lambda image: image.sample(region=sampleRegion.geometry(),\
                            projection=image.select(image.slice(4,5).bandNames()).projection(), scale=outputScale,geometries=False, dropNulls = True, factor=factor) ).flatten()

    return sampleData

# produce a image collection  corresponding to output time series of images for  site 
def getImages(site,
               variable,
               collectionOptions,
               networkOptions,
               maxCloudcover,
               bufferSpatialSize,
               inputScale,
               startDate,
               endDate,
               start,
               end,
               field,
               outputScale):
 
       
    # Buffer features ifs requested
    if ( bufferSpatialSize > 0 ):
        site = ee.Feature(site).buffer(bufferSpatialSize)

    # Initialize feature collection of samples with only the site geometry and no samples
    outputImageCollection = ee.ImageCollection(site.geometry())


     # make image collection of output products for sampling
    outputImageCollection = ee.ImageCollection(makeProductCollection(collectionOptions,
                                                                 networkOptions,
                                                                 variable,
                                                                 site.geometry(),
                                                                 startDate,
                                                                 endDate,
                                                                 start,
                                                                 end,
                                                                 field,
                                                                 maxCloudcover,
                                                                 inputScale,
                                                                 outputScale))


    return  outputImageCollection


# produce a feature collection of samples corresponding to output time series for sampled pixels in a  site 
def getSamples(site,
               variable,
               collectionOptions,
               networkOptions,
               maxCloudcover,
               bufferSpatialSize,
               inputScale,
               startDate,
               endDate,
               start,
               end,
               field,
               outputScale,
               factor=1,
               numPixels=0):
 
    # Buffer features ifs requested
    if ( bufferSpatialSize > 0 ):
        site = ee.Feature(site).buffer(bufferSpatialSize)

    # Initialize feature collection of samples with only the site geometry and no samples
    outputFeatureCollection = ee.FeatureCollection(site.geometry())

     # make image collection of output products for sampling
    productCollection = ee.ImageCollection(makeProductCollection(collectionOptions,
                                                                 networkOptions,
                                                                 variable,
                                                                 site.geometry(),
                                                                 startDate,
                                                                 endDate,
                                                                 start,
                                                                 end,
                                                                 field,
                                                                 maxCloudcover,
                                                                 inputScale,
                                                                 outputScale))

    # produce samples for each pixel within the site only if there is at least one image
    if productCollection :
        if ( productCollection.size().getInfo() > 0 ):
            outputFeatureCollection = ee.FeatureCollection(sampleProductCollection(productCollection, site.geometry(),  outputScale,factor,numPixels))

    return  outputFeatureCollection


 #format samples into a data frame if client site output is requested
def samplestoDF(sampleFeatureCollection):

    # create empty data frame to hold samples
    sampleDF = pd.DataFrame()
  
    sampleList= ee.List(sampleFeatureCollection.propertyNames().map(lambda propertyName: ee.Dictionary({ 'propertyName': propertyName, 'data': sampleFeatureCollection.aggregate_array(propertyName)})))

    for col in sampleList:
        df = pd.DataFrame((col['data']),columns=[col['bandName']])
        if (not(df.empty)) :
            sampleDF = pd.concat([sampleDF,df],axis=1) 
    
    if  (not(sampleDF.empty)) :
        sampleDF = sampleDF.dropna(subset=['date'])

    return sampleDF


# create date range from time start and time end  of each input feature
def createDates(site,bufferTemporalSize,timeZone):
                
    time_start = site.get('system:time_start').getInfo()

    # if the date is not in unix epoch format we assume it is in a specified time zone
    if isinstance(time_start, int):
        startDate = datetime.fromtimestamp(site.get('system:time_start').getInfo()/1000) + timedelta(days=bufferTemporalSize[0])
    else:
        startDate = datetime.fromtimestamp(ee.Date.parse("YYYY-MM-dd",site.get('system:time_start'),'Etc/GMT+6').getInfo()['value']/1000) +timedelta(days=bufferTemporalSize[0])

    if ("system:time_end" in site.propertyNames().getInfo()):
        time_end = site.get('system:time_end').getInfo()
        if isinstance(time_end, int):
            endDate = datetime.fromtimestamp(site.get('system:time_end').getInfo()/1000) + timedelta(days=bufferTemporalSize[1])
        else:
            endDate = datetime.fromtimestamp(ee.Date.parse("YYYY-MM-dd",site.get('system:time_end'),timeZone).getInfo()['value']/1000) +timedelta(days=bufferTemporalSize[1])
    else:
        endDate = startDate - timedelta(days=bufferTemporalSize[0]) + timedelta(days=bufferTemporalSize[1])
    return startDate, endDate + timedelta(days=1)


# split a time interval into a list of non-overlapping dates
def splitDates(startDate,endDate,shardSize):

    endDates = pd.date_range(startDate,endDate,freq=shardSize).to_frame(index=False,name='endDate')[:-1]
    startDates = endDates.rename(columns={'endDate':'startDate'})
    startDates = pd.concat([pd.DataFrame([startDate],columns=['startDate']),startDates],axis=0).reset_index(drop=True)
    endDates = pd.concat([endDates,pd.DataFrame([endDate],columns=['endDate'])],axis=0).reset_index(drop=True)

    return startDates.join(endDates)


# produce output for a temporal and spatial sub-sample features for all sites in a list of provided GEE feature collections
# results are saved either as .csv files on Google (GEE asset or Google Drive or Google Cloud Platform)
# or return to the client as a  Local pandas DF 
#
def sampleSites(siteList,
                imageCollectionName,
                sensorName,
                outputPath,
                algorithm,
                variableName='surface_reflectance',
                featureRange=[0,np.nan],
                maxCloudcover=100,
                outputScale=30,
                exportDevice='Asset',
                prefixProperty='system:index',
                inputScale=30,
                bufferSpatialSize=0,
                subsamplingFraction=1,
                numPixels=0,
                bufferTemporalSize=[0,0],
                calendarRange=[1,12,'month'],
                timeZone='Etc/GMT+6',
                shardSize='ys'
                ):

    print('STARTING LEAF.sampleSites using ',imageCollectionName)
 

    # parse the start and end date for sampling pixels in each site
    if (type(bufferTemporalSize[0])==str):
        try: 
            startDate = datetime.strptime(bufferTemporalSize[0],"%Y-%m-%d")
            endDate =  datetime.strptime(bufferTemporalSize[1],"%Y-%m-%d")
            endDatePlusOne = endDate + timedelta(days=1)
            defaultDate=True
        except ValueError:
            defaultDate = False
    else:
        defaultDate = False
    calendarStart = calendarRange[0]
    calendarEnd = calendarRange[1]
    calendarField = calendarRange[2]
  
    #parse feature range
    featureRange = np.sort(featureRange)

    # specify sensor and algorithm options
    collectionOptions = dictionariesSL2P.make_collection_options(algorithm)
    networkOptions= dictionariesSL2P.make_net_options()


    # sample all provided feature collections of sites 
    for input in siteList:   

        # Convert the feature collection to a list so we can apply SL2P on features in sequence to avoid time outs on GEE
        sampleRecords =  ee.FeatureCollection(input).sort('system:time_start', False).map(lambda feature: feature.set('timeStart',feature.get('system:time_start')))
        sampleRecords =  sampleRecords.toList(sampleRecords.size())
        print('Site: ',input, ' with ',sampleRecords.size().getInfo(), ' features.')

        # determine appropriate range of features for this site
        featureRange[0] =np.int32(np.nanmax([featureRange[0],0]))
        featureRange[1]=np.int32(np.nanmin([featureRange[1],sampleRecords.size().getInfo()]))

        #  if local output initalize empty list for outputs, define output file name  , and write metadata of this run       
        if exportDevice==None:
                raise ValueError("Output Drive/Asset Folder/CloudStorage Path missing.")
            

        # start sampling features requested from each site
        print('Processing Features: from %s to %s'%(featureRange[0],featureRange[1]))
        for n in range(featureRange[0],featureRange[1]+1) :

            # select feature to process
            site = ee.Feature(sampleRecords.get(n))

            # get start and end date for this feature if a date range is not specified
            if ( defaultDate==False ):
                startDate, endDate = createDates(site,bufferTemporalSize,timeZone)

            print('Feature n°: %s/%s  -- startDate: %s -- endDate: %s'%(n,featureRange[1],startDate,endDate))
            print('----------------------------------------------------------------------------------------------------------')
        

            # define date range for export sharding to avoid memory and compute limit        
 
            # sample in temporal shards 
            if (len(pd.date_range(startDate,endDate,freq=shardSize)) > 0 ) & (defaultDate==True):
                dateRange = splitDates(startDate,endDate,shardSize)
            else:
                dateRange = pd.DataFrame( {'startDate':[startDate],'endDate':[endDate]})   

            # iterate over all dateRange shards and produce output samples             
            for index, Dates in dateRange.iterrows():

                print('Sampling from ',Dates['startDate'],' to ',Dates['endDate'])
                
                # get a feature collection of samples, one feature per sampled image, properties correspond to product layers of image               
                outputFeatureCollection=getSamples(site,
                                                   variableName,
                                                   collectionOptions[imageCollectionName],
                                                   networkOptions[variableName][imageCollectionName],
                                                   maxCloudcover,
                                                   bufferSpatialSize,
                                                   inputScale,
                                                   Dates['startDate'],
                                                   Dates['endDate']+ timedelta(days=1),
                                                   calendarStart,
                                                   calendarEnd,
                                                   calendarField,
                                                   outputScale,
                                                   subsamplingFraction,
                                                   numPixels)
                    
                # prepare feature for export if it exists
                if outputFeatureCollection :
                    #update client side DF if requested
                    if ( exportDevice in ['Client'] ):
                        samplesDF = pd.concat([samplesDF,samplestoDF(outputFeatureCollection)],ignore_index=True)

                    if  ( exportDevice in ['Drive'] ):
            
                        # Define the export parameters
                        export_params = {
                            'collection' : outputFeatureCollection,
                            'description' : sensorName+variableName+'_'+site.get(prefixProperty).getInfo()+'_'+Dates['startDate'].strftime("%Y%m%d")+'_'+Dates['endDate'].strftime("%Y%m%d"),
                            'fileNamePrefix' : sensorName+variableName+'_'+site.get(prefixProperty).getInfo()+'_'+Dates['startDate'].strftime("%Y%m%d")+'_'+Dates['endDate'].strftime("%Y%m%d"),
                            'folder': outputPath
                        }
                        task = ee.batch.Export.table.toDrive(**export_params)

                    if  ( exportDevice in ['CloudStorage'] ):
            
                        # Define the export parameters
                        export_params = {
                        'collection' : outputFeatureCollection,
                        'description' : sensorName+variableName+'_'+site.get(prefixProperty).getInfo()+'_'+Dates['startDate'].strftime("%Y%m%d")+'_'+Dates['endDate'].strftime("%Y%m%d"),
                        'fileNamePrefix' : sensorName+variableName+'_'+site.get(prefixProperty).getInfo()+'_'+Dates['startDate'].strftime("%Y%m%d")+'_'+Dates['endDate'].strftime("%Y%m%d"),
                        'bucket' : outputPath
                        }
                        task = ee.batch.Export.table.toCloudStorage(**export_params)
        
                    if  ( exportDevice in ['Asset'] ):
            
                        # Define the export parameters
                        export_params = {
                            'collection' : sampleFeatureCollection,
                            'description' : sensorName+variableName + '_'+site.get(prefixProperty).getInfo()+'_'+Dates['startDate'].strftime("%Y%m%d")+'_'+Dates['endDate'].strftime("%Y%m%d"),
                            'assetId' : outputPath + sensorName+variableName + '_'+site.get(prefixProperty).getInfo()+'_'+Dates['startDate'].strftime("%Y%m%d")+'_'+Dates['endDate'].strftime("%Y%m%d"),
                        }
                        task = ee.batch.Export.table.toAsset(**export_params)

                    if ( exportDevice in ['Asset','Drive','CloudStorage'] ) :
                    
                        # Start the export task
                        task.start()
                        
                        # You can optionally print the task ID to monitor its progress in the Earth Engine Code Editor's Tasks tab
                        print(f"Export task started with ID: {task.id}")


                
        print('\nDONE LEAF.sampleSites\n')

        return True


# produce output for a temporal and spatial sub-sample features for all sites in a list of provided GEE feature collections
# results are saved either as .csv files on Google (GEE asset or Google Drive or Google Cloud Platform)
# or return to the client as a  Local pandas DF 
#
def imageSites(siteList,
                imageCollectionName,
                sensorName,
                outputPath,
                algorithm,
                variableName='surface_reflectance',
                featureRange=[0,np.nan],
                maxCloudcover=100,
                outputScale=30,
                exportDevice='Asset',
                prefixProperty='system:index',
                inputScale=30,
                bufferSpatialSize=0,
                subsamplingFraction=1,
                numPixels=0,
                bufferTemporalSize=[0,0],
                calendarRange=[1,12,'month'],
                timeZone='Etc/GMT+6',
                shardSize='ys',
                exportDataType='float'
                ):
    
    print('STARTING LEAF.imageSites using ',imageCollectionName)
 
    # parse the start and end date for sampling pixels in each site
    if (type(bufferTemporalSize[0])==str):
        try: 
            startDate = datetime.strptime(bufferTemporalSize[0],"%Y-%m-%d")
            endDate =  datetime.strptime(bufferTemporalSize[1],"%Y-%m-%d")
            endDatePlusOne = endDate + timedelta(days=1)
            defaultDate=True
        except ValueError:
            defaultDate = False
    else:
        defaultDate = False
    calendarStart = calendarRange[0]
    calendarEnd = calendarRange[1]
    calendarField = calendarRange[2]
  

    
    #parse feature range
    featureRange = np.sort(featureRange)

    # specify sensor and algorithm options
    collectionOptions = dictionariesSL2P.make_collection_options(algorithm)
    networkOptions= dictionariesSL2P.make_net_options()


    # sample all provided feature collections of sites 
    for input in siteList:   

        # Convert the feature collection to a list so we can apply SL2P on features in sequence to avoid time outs on GEE
        sampleRecords =  ee.FeatureCollection(input).sort('system:time_start', False).map(lambda feature: feature.set('timeStart',feature.get('system:time_start')))
        sampleRecords =  sampleRecords.toList(sampleRecords.size())
        print('Site: ',input, ' with ',sampleRecords.size().getInfo(), ' features.')

        # determine appropriate range of features for this site
        featureRange[0] =np.int32(np.nanmax([featureRange[0],0]))
        featureRange[1]=np.int32(np.nanmin([featureRange[1],sampleRecords.size().getInfo()]))

        # initialize output
        if outputPath==None:
            raise ValueError("Output Drive/Asset Folder/CloudStorage Path missing.")
            

        # start sampling features requested from each site
        print('Processing Features: from %s to %s'%(featureRange[0],featureRange[1]))
        for n in range(featureRange[0],featureRange[1]+1) :

            # select feature to process
            site = ee.Feature(sampleRecords.get(n))

            # get start and end date for this feature if a date range is not specified
            if ( defaultDate==False ):
                startDate, endDate = createDates(site,bufferTemporalSize,timeZone)

            print('Feature n°: %s/%s  -- startDate: %s -- endDate: %s'%(n,featureRange[1],startDate,endDate))
            print('----------------------------------------------------------------------------------------------------------')
        

            # define date range for export sharding to avoid memory and compute limit        
 
            # sample in temporal shards 
            if (len(pd.date_range(startDate,endDate,freq=shardSize)) > 0 ) & (defaultDate==True):
                dateRange = splitDates(startDate,endDate,shardSize)
            else:
                dateRange = pd.DataFrame( {'startDate':[startDate],'endDate':[endDate]})   

            # iterate over all dateRange shards and produce output samples             
            for index, Dates in dateRange.iterrows():

                print('Mapping from ',Dates['startDate'],' to ',Dates['endDate'])
                
                # get a feature collection of samples, one feature per sampled image, properties correspond to product layers of image               
                outputImageCollection=getImages(site,
                                                   variableName,
                                                   collectionOptions[imageCollectionName],
                                                   networkOptions[variableName][imageCollectionName],
                                                   maxCloudcover,
                                                   bufferSpatialSize,
                                                   inputScale,
                                                   Dates['startDate'],
                                                   Dates['endDate']+ timedelta(days=1),
                                                   calendarStart,
                                                   calendarEnd,
                                                   calendarField,
                                                   outputScale)

                # prepare feature for export if it exists
                if outputImageCollection :


                    if  ( exportDevice in ['Drive'] ):
            
                        # Define the export parameters
                        export_params = {
                            'imagecollection' : outputImageCollection.map(lambda image: image.float()),
                            'index_property' : prefixProperty,
                            'description' :  sensorName + variableName+'_'+site.get(prefixProperty).getInfo()+'_'+Dates['startDate'].strftime("%Y%m%d")+'_'+Dates['endDate'].strftime("%Y%m%d"),
                            'folder' : outputPath,
                            'region' : outputImageCollection.geometry(),
                            'scale' : outputScale,
                            'maxPixels' : 1e13,
                            'fileNamePrefix' : sensorName + variableName+'_'+site.get(prefixProperty).getInfo()+'_'+Dates['startDate'].strftime("%Y%m%d")+'_'+Dates['endDate'].strftime("%Y%m%d"),
                        }
                        tasks = ee.batch.Export.geetools.imagecollection.toDrive(**export_params)

                    if  ( exportDevice in ['CloudStorage'] ):
            
                        # Define the export parameters
                        export_params = {
                            'imagecollection' : outputImageCollection.map(lambda image: image.float()),
                            'index_property' : prefixProperty,
                            'description' :  sensorName + variableName+'_'+site.get(prefixProperty).getInfo()+'_'+Dates['startDate'].strftime("%Y%m%d")+'_'+Dates['endDate'].strftime("%Y%m%d"),
                            'bucket' : outputPath,
                            'region' : outputImageCollection.geometry(),
                            'scale' : outputScale,
                            'maxPixels' : 1e13,
                            'fileNamePrefix' : sensorName + variableName+'_'+site.get(prefixProperty).getInfo()+'_'+Dates['startDate'].strftime("%Y%m%d")+'_'+Dates['endDate'].strftime("%Y%m%d"),

                        }
                        tasks = ee.batch.Export.geetools.imagecollection.toCloudStorage(**export_params)

        
                    if  ( exportDevice in ['Asset'] ):
            
                        # Define the export parameters
                        export_params = {
                            'imagecollection' : outputImageCollection.map(lambda image: image.float()),
                            'index_property' : prefixProperty,
                            'description' :  sensorName + variableName+'_'+site.get(prefixProperty).getInfo()+'_'+Dates['startDate'].strftime("%Y%m%d")+'_'+Dates['endDate'].strftime("%Y%m%d"),
                            'assetID' : outputPath + sensorName + variableName+'_'+site.get(prefixProperty).getInfo()+'_'+Dates['startDate'].strftime("%Y%m%d")+'_'+Dates['endDate'].strftime("%Y%m%d"),
                            'region' : outputImageCollection.geometry(),
                            'scale' : outputScale,
                            'maxPixels' : 1e13,
                        }
                        tasks = ee.batch.Export.geetools.imagecollection.toCAsset(**export_params)

                    if ( exportDevice in ['Asset','Drive','CloudStorage'] ) :

                 
                        # Start the export task
                        for task in tasks:

                            task.start()
                        
                            # You can optionally print the task ID to monitor its progress in the Earth Engine Code Editor's Tasks tab
                            print(f"Export task started with ID: {task.id}")

                
        print('\nDONE LEAF.sites\n')
    return True
