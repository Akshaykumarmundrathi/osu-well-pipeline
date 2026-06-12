# Embedded-text-layer coverage (free extraction potential)

Honest signal = a FILLED value in the layer, not blank-form template or address boilerplate.
  • clean text  = digital-native layer (not OCR garbage)
  • STR value   = township/range/section label glued to a number
  • county value= a real well-county (non-Oklahoma, or 'COUNTY <name>')
  • FREE WIN    = clean AND STR-value AND county-value (extractable, $0)

| coll | n | has text | clean text | STR value | county value | FREE WIN |
|--|--|--|--|--|--|--|
| C1 | 80 | 96% | 20% | 42% | 15% | **3%** |
| C2 | 80 | 95% | 25% | 51% | 15% | **2%** |
| C3 | 80 | 95% | 51% | 52% | 22% | **8%** |
| C4 | 80 | 98% | 83% | 63% | 50% | **32%** |
| C5 | 80 | 98% | 91% | 56% | 37% | **26%** |
| C6 | 80 | 98% | 92% | 68% | 41% | **36%** |
| C7 | 80 | 98% | 78% | 63% | 33% | **23%** |
| C8 | 80 | 98% | 95% | 51% | 45% | **27%** |
| C9 | 80 | 96% | 96% | 61% | 37% | **25%** |
| C10 | 80 | 96% | 95% | 65% | 48% | **32%** |
| C11 | 80 | 63% | 63% | 20% | 46% | **17%** |
| C12 | 80 | 98% | 98% | 8% | 98% | **8%** |
| C13 | 80 | 90% | 90% | 11% | 90% | **11%** |

## Sample FREE WINs (clean text + filled STR + real county)

- **C1** `X_WICHITA FEE_2273099` -> county **Washington** -- `\ 7 4p ` 9~C~ 0 J U I 0 0 3 :~ ~ $ WELL RECOR Washington Co„nty Company The Prairie Cil & as Co . Farm Wichita Fee Lot Section 1113 Township 28 Range 13 Well No 43. . _ Drilling Commenced February 9 t`
- **C1** `01910511_HUXIE 13 (HEALDTON I UNIT 9-13)_914043` -> county **Carter** -- `0 8 0 0 .a.....,,~.o...... ' WELL RE C O R Carter Councy Comp any S cl ir Oil and Gas Comnany s Farm Fi iX; c ►nt 3ection 31 Township 3 Range 3 {pell No. 13 Drilling Commenced T~0 rch ll.th 1916 Drill`
- **C2** `10302613_R M HANNAH 1_1542975` -> county **Noble** -- `CO RPORATION COM M ISSIO N WELL LOG DIVISION, ; OKLAHOMA CIT Y, O KLAHOMA 0 2 SSODLE . ~ s ~ + FCOhIPANY- Skelly Oil Company SEC. - FARM- R. b'4 'Hannah No . 1 -~- ' •--~ --~---- -- LOCATION- NE S tiV`
- **C2** `10506651_CLYDE V TANNER 33_1622048` -> county **Tulsa** -- `640 Acre s ~ z d ° N 1) I /Cou= ~~,'dELL F;E CO 1VTail to Corporation Commission, Oklahoma City, Oklahoma _ _ NOwa'Ea -- -- - - - SEC --- 36 -. TWP --- 27 -_, RGE 16 _ COMPANY OPERATING _ S3 IIC1B.l i`
- **C3** `13703876_FRENSLEY D-3_930482` -> county **Stephens** -- `t J s'~ , tS nn'nTr~ ~ c ?r~ T ;F CdMP,4NI' OPERATING ~sell~ull ~or~~env \~ '~^1t • ''- OFFICE ADDRESS B4x 44_Je1mR Oklaho;na FARM NAME -.J . R . Fre rv^.10 "D" ~_ .T._ - WELL NO . 25 DRILLING STARTED`
- **C3** `01537507_POHLEMANN 1_785865` -> county **Noble** -- `p C0 ail o ~a~rofion ~ con Pexr OPERATING __- 51 Form laul Oklahoma City, Oklahoma ___, TWP L_Nosj b.., xas _1S2S1ast -.har s I OPFICE ADDRESS """'""F """" "'"'"""""'-"-""""""""" PAFLM NAME "'P.9YL1_e`
- **C4** `10570100_ALBERT TANNER BW-3_1637906` -> county **Nowata** -- `W ~ ~ 4i ~ zti N r CJ ~ a 4j 1'{ 0 pz Form la',2q •~v ( M m1 t o Corporation Comm iuio n Oklahoma Ciir O klahoma ) ~f OKLAHOMA CORPORATION COMMISSION 973 OIL AND GAS COhS EAVA T70N DEPARTMENT HO Aczes`
- **C4** `10301155_LIVELY 1_1264676` -> county **Noble** -- `~ O W W Form 10P3A (Mail to Corpomhon Commi wion Oklahoma City Oklahoma) FORMATION RECORD OKLAHOMA CORPORATION COMMISSION 150 7 O IL AND GAS CONSERVATION DEPARTMENT Give detailed description and thick`
- **C5** `00735247_H OVERTON UNIT 1_730640` -> county **Tulsa** -- `U -~ -- +Form 1002 A N ~ 6b Acre3 ai N M ti ~ Uw~ T o y rio z - 0U ~ OKLAHOMA CORPORATION COMMISSION O IL AND GAS CONSERVATION D EPA RTME N T WELL RECO tD ~., COUNTY Beayer SEC 17 TWP 3N RGE 2U++r+IS `
- **C5** `OSAGE_VIVIAN SIMPKINS 1 A_2323546` -> county **Osage** -- `(Mail to Corporation Commission Ok lahoma Cit y Oklahoma) FORMATION RECORD OKLAHOMA CORPORATION COMM ISSION Give detailed description and thickness of all lormat,ons dnlled through contents of sand wh`
- **C6** `05136696_L H HARRISON 11_804865` -> county **Grady** -- `~ po~m ] 002 A , a N Cl) M N 0w (Mail to Corpomnoo Commir von Ok lahoma City, Oklahoma ) OKLAHOMA CORPORATION COMMISSION OIL AND GAS CONSERVATION DEPARTMENT WELL RECORD COUNTY Grady SEC 28 ,TwP 3N RGE`
- **C6** `X_UNKNOWN 1_1466733` -> county **Osage** -- `1 '~i O 04 N w V1 Form IOOP A Bf0 Acres r ( Mmi to Corporation Commw w n, Oklahoma City Oklahoma ) OKLAHOMA CORPORATION COMM ISS ION OIL A ND GA S CONSERVATION DEPARTMEN T WELL 9ECOH D COUNTY Osage SE`
- **C7** `01921031_N KELLER UNIT 2-1 (MCGILL 1)_960011` -> county **Carter** -- `/OCC Op er No . 01 This form is an O riv inal = Amended ED .. .. ~,, . . 11. 10 30 . .. . .,,. . .. ill ,. . ,. .... 1 . 1 . . OKI.AHOMA CORPORATI ON COMMI SS IO N unit No . OIL AND GAS CONSERVATION D`
- **C7** `05120771_WINHAM 1-23_815660` -> county **Grady** -- `10A . OTC/UCC Op . r :b . 04309 This fon Si an Oritinel d Am ended Q i.'API . 05120771 1* 6 . ~~u d .u~ l . 30 den ~u or u 6 u 189 Is s omru se • OKLAMOMA CORPORATION COMMISSION 2 . oiC rroJ . Un ic n`
- **C8** `01722420_JENSEN 3-28_1293561` -> county **Atoka** -- `At 19" OIL AND GAS CONSERVATION DIVISION LEASE NO ~ Z 3 Jim Thorpe Building i Oklahoma C Ny. Oklahoma 73105 „ --------~ -~~---r-~_-_ at 73108 AUEt1dCd for Well IVffi e CFuingEE . PLEAS~E 7T-Y~ PE OR U`
- **C8** `15321373_HONEYSUCKLE 1_503594` -> county **Tulsa** -- `AMENDED TO SHOW CHANGE OF OPERATOEI'LEASE TYPE OR USE BLACK INK ON LY (To be filed within 30 tlavs after tl .Jlmg is compleieA ) 1002q OK LAHOMA CORPORATION COMMI SS ION OTC COUN7V CO MPLETION & TEST `
- **C9** `11922600_DVORAK 1_1181160` -> county **Choctaw** -- `PLEASE TYPE OR USE BLACK INK ONL Y ~ (To b e filed within 30 days after drilling is completed ) OKLAHOMA CORPORATION COMMISSION O7C COUNTY COMPLETION & TEST DATA BY PRODUCING FORMATI O N ne:m1s~s'A OI`
- **C9** `08122785_SUMMIT 1_1186323` -> county **CLEVELAND** -- `10A. o'cc/GCC Oper No . 14636 This fozm is an orieinal W qmended C] Its Thorpe building / Oklrhoms City, Oklahoma 73105-4993 r ,' • _ ~ 1. NO 1 ' 1 l • I N . a 30 0 1 1 . . iN , ~nlll~~ ~ co~I~~ • •KL`
- **C10** `12121712_PEARL DAVIS 4_569571` -> county **Pittsburg** -- `~ I OA OT C /OCC Oper No 03492-0 This fo rm is an Onvi n al M Amended Q I Nu m er 121 ' 217 12 To be nua iin i nao e.r . .i f .. a ff nii no is ceT O i . t . a OKLAHOMA CORPORATION COMMISSION Je.~~"f `
- **C10** `01522363_HENDRICKS 1-10_858205` -> county **Caddo** -- `NPI NO Rule 165 :I8-3-23 015-22363 ORIGINAL O ~(~ i rl J RMEN ~EU O T C PROD UN IT NO Reason P iended : 015-099944 D lERSE T YDE OA USE BLRLN IH l QLY ~ TYPE OF DRILLING OPERATION : r A R l GHT FIOLE `
- **C11** `129236840000_HAVEN 34-16-26 1H_500218426` -> county **ROGER MILLS** -- `January 17, 2013 1 of 2 OTC Prod. Unit No.: 129-208840 Completion Report Well Name: HAVEN 34-16-26 1H Min Gas Allowable: Yes Purchaser/Measurer: CHESAPEAKE OPERATING  INC First Sales Date: 09/27/2012 `
- **C11** `137268950000_HEFNER 18-12_500198849` -> county **STEPHENS** -- `September 14, 2012 1 of 2 OTC Prod. Unit No.: Completion Report Well Name: HEFNER 18-12 Purchaser/Measurer: First Sales Date: There are no Plug records to display. Depth Plug Type API No.: 35137268950`
- **C12** `103246100000_WOSMEK 23-20N-1W 1SWD_500292093` -> county **NOBLE** -- `April 11, 2014 1 of 2 OTC Prod. Unit No.: Completion Report Well Name: WOSMEK 23-20N-1W 1SWD Purchaser/Measurer: First Sales Date: 7290 CEMENT Depth Plug Type API No.: 35103246100000 X Single Zone Mul`
- **C12** `017245320002_BEARD 34-13N-9W 1H_500259860` -> county **CANADIAN** -- `July 30, 2013 1 of 3 OTC Prod. Unit No.: 017210592 Amended Completion Report Well Name: BEARD 34-13N-9W 1H Min Gas Allowable: Yes Purchaser/Measurer: DEVON GAS SERVICES First Sales Date: 05/24/2013 Th`
- **C13** `011241120000_OPPEL 1610 5H-24X_500828599` -> county **BLAINE** -- `OTC Prod. Unit No.: 011-225507-0-0000 Completion Report Well Name: OPPEL 1610 5H-24X Min Gas Allowable: Yes Purchaser/Measurer: MUSTANG GAS PRODUCTS  LLC First Sales Date: 4/28/19 Depth Plug Type Ther`
- **C13** `137276080000_AOSU 3-24_500902392` -> county **STEPHENS** -- `OTC Prod. Unit No.:  Completion Report Well Name: AOSU 3-24 Purchaser/Measurer:  First Sales Date:  Depth Plug Type There are no Plug records to display. API No.: 35137276080000 X Single Zone Multiple`
