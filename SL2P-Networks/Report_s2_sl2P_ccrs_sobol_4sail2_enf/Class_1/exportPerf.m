function [perf]=exportPerf(perfStruct)
    LAIvalid = perfStruct.LAI.Valid;
    LAIestime = perfStruct.LAI.Estime;
    FAPARvalid = perfStruct.FAPAR.Valid;
    FAPARestime = perfStruct.FAPAR.Estime;
    LAICwvalid = perfStruct.LAI_Cw.Valid;
    LAICwestime = perfStruct.LAI_Cw.Estime;
    FCOVERvalid = perfStruct.FCOVER.Valid;
    FCOVERestime = perfStruct.FCOVER.Estime; 
    LAICabvalid = perfStruct.LAI_Cab.Valid;
    LAICabestime = perfStruct.LAI_Cab.Estime;
    Albedovalid = perfStruct.Albedo.Valid;
    Albedoestime = perfStruct.Albedo.Estime;  
    Dvalid = perfStruct.D.Valid;
    Destime = perfStruct.D.Estime;  
    perf = table(LAIvalid,LAIestime,FAPARvalid,FAPARestime,FCOVERvalid,FCOVERestime,Albedovalid,Albedoestime,LAICabvalid,LAICabestime,LAICwvalid,LAICwestime,Dvalid,Destime);
return
    